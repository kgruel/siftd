"""CLI handlers for 'siftd db' namespace — container-level operations.

Commands that operate on the database container itself:
info, stats, workspaces, path, vacuum, backup, restore, slice, merge,
remote (add/list/remove), push.

Distinct from top-level commands (query, search, export, tag, peek)
which are user workflows selecting conversations.
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from siftd.cli._common import resolve_db
from siftd.dateparse import parse_date
from siftd.errors import SiftdError
from siftd.output import fmt_count, status


def _database_artifacts(db_path: Path) -> list[Path]:
    """Return the SQLite database file and its WAL sidecars."""
    return [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]


_SUMMARY_TABLES = ("conversations", "events", "tags", "content_blobs")


def _table_row_counts(conn) -> dict[str, int]:
    existing = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in _SUMMARY_TABLES
        if t in existing
    }


def cmd_db_info(args) -> int:
    """Show database file metadata and schema information."""
    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    from siftd.api import open_database

    # Diagnostic command — report what's on disk; don't auto-upgrade as a side effect.
    conn = open_database(db, read_only=True, auto_upgrade=False)
    try:
        size_bytes = db.stat().st_size
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # Check for FTS5 table
        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone() is not None
    finally:
        conn.close()

    from siftd.output.listing import StatusReport
    from siftd.output.theme import domain_styles

    ds = domain_styles()
    report = StatusReport()
    # Counts ride the amber metric thread (the page/byte tallies); the KB size
    # stays plain to match the sibling ``db vacuum`` report. Path / mode / version
    # are plain facts.
    report.preamble(
        {
            "Path": str(db),
            "Size": [
                (f"{size_bytes / 1024:.1f} KB (", None),
                (fmt_count(size_bytes), ds.metric),
                (" bytes)", None),
            ],
            "Page size": [(fmt_count(page_size), ds.metric), (" bytes", None)],
            "Page count": [(fmt_count(page_count), ds.metric)],
            "Journal mode": journal_mode,
            "Schema version": str(user_version),
            "FTS5 index": "yes" if fts_exists else "no",
        }
    )
    report.render()
    return 0


def cmd_db_schema_version(args) -> int:
    """Show migration triage info: current version, target, and pending migrations."""
    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    from siftd.api import open_database
    from siftd.api.migrations import get_schema_version_info

    # Triage command — must report the on-disk version even when stale.
    conn = open_database(db, read_only=True, auto_upgrade=False)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()

    info = get_schema_version_info(current)
    target = info["target_version"]
    pending = info["pending"]
    all_migrations = info["all_migrations"]

    from siftd.output.listing import StatusReport
    from siftd.output.status import severity_mark

    if current > target:
        if getattr(args, "json", False):
            print(json.dumps({
                **info,
                "error": (
                    f"DB schema version {current} is newer than this siftd "
                    f"supports (max {target}). Upgrade siftd."
                ),
            }))
        else:
            glyph, sev_style = severity_mark("error")
            report = StatusReport()
            report.preamble(
                {
                    "Current version": str(current),
                    "Target version": str(target),
                    "Status": [(
                        f"{glyph} ERROR: DB version {current} exceeds supported "
                        f"max {target} — upgrade siftd.",
                        sev_style,
                    )],
                }
            )
            report.render()
        return 1

    if getattr(args, "json", False):
        print(json.dumps(info))
        return 0

    if not pending:
        status_text, severity = "up to date", None
    elif len(pending) == 1:
        status_text, severity = "1 migration pending", "warning"
    else:
        status_text, severity = f"{len(pending)} migrations pending", "warning"
    glyph, sev_style = severity_mark(severity)

    report = StatusReport()
    report.preamble(
        {
            "Current version": str(current),
            "Target version": str(target),
            "Status": [(f"{glyph} {status_text}", sev_style)],
        }
    )
    report.lines_section(
        "Registered migrations",
        [f"v{v} ({'pending' if v > current else 'applied'})" for v in all_migrations],
    )
    if pending:
        report.note("Run 'siftd ingest' to apply pending migrations.")
    report.render()
    return 0


def cmd_db_stats(args) -> int:
    """Show database statistics (delegates to status implementation)."""
    from siftd.cli.meta import cmd_status

    return cmd_status(args)


def cmd_db_workspaces(args) -> int:
    """List workspaces (delegates to workspaces implementation)."""
    from siftd.cli.meta import cmd_workspaces

    return cmd_workspaces(args)


def cmd_db_path(args) -> int:
    """Show XDG paths (delegates to path implementation)."""
    from siftd.cli.meta import cmd_path

    return cmd_path(args)


def cmd_db_vacuum(args) -> int:
    """Compact the database and optimize indexes."""
    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    size_before = db.stat().st_size

    from siftd.api import open_database

    conn = open_database(db)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    size_after = db.stat().st_size
    saved = size_before - size_after

    from siftd.output.listing import StatusReport

    pairs = {
        "Before": f"{size_before / 1024:.1f} KB",
        "After": f"{size_after / 1024:.1f} KB",
    }
    if saved > 0:
        pairs["Saved"] = f"{saved / 1024:.1f} KB ({saved / size_before * 100:.1f}%)"
    report = StatusReport()
    report.preamble(pairs)
    if saved <= 0:
        report.note("No space reclaimed (database already compact).")
    report.render()
    return 0


def cmd_db_backup(args) -> int:
    """Create a consistent backup of the database."""
    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    target = Path(args.output)
    if target.exists() and not args.force:
        status.error(f"Target already exists: {target}", hint="Use --force to overwrite.")
        return 1

    if target.exists():
        target.unlink()

    from siftd.api.database import backup_database

    backup_database(db, target)

    size = target.stat().st_size
    status.confirm(f"Backed up to: {target} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_restore(args) -> int:
    """Restore the database from a backup file."""
    source = Path(args.input)

    if not source.exists():
        status.error(f"Backup file not found: {source}")
        return 1

    # Validate SQLite magic bytes
    with open(source, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3\x00"):
        status.error(f"Not a valid SQLite database: {source}")
        return 1

    db = resolve_db(args)

    if getattr(args, "dry_run", False):
        from siftd.api import open_database

        tgt_schema_ver = None
        tgt_counts: dict[str, int] = {}
        if db.exists():
            conn2 = open_database(db, read_only=True, auto_upgrade=False)
            try:
                tgt_schema_ver = conn2.execute("PRAGMA user_version").fetchone()[0]
                tgt_counts = _table_row_counts(conn2)
            finally:
                conn2.close()
        conn = open_database(source, read_only=True, auto_upgrade=False)
        try:
            src_schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
            src_counts = _table_row_counts(conn)
        finally:
            conn.close()
        from siftd.output.common import prefers_ascii
        from siftd.output.listing import print_definitions, print_heading
        from siftd.output.status import severity_mark
        from siftd.output.table import print_table

        downgrade = False
        if tgt_schema_ver is None:
            schema = f"v{src_schema_ver} (target does not exist)"
        elif tgt_schema_ver == src_schema_ver:
            schema = f"v{src_schema_ver} (no change)"
        elif src_schema_ver > tgt_schema_ver:
            schema = f"v{tgt_schema_ver} → v{src_schema_ver} (upgrade)"
        else:
            schema = f"v{tgt_schema_ver} → v{src_schema_ver} (DOWNGRADE)"
            downgrade = True

        as_ascii = prefers_ascii()
        # None is the all-clear ✓; a downgrade earns a warning-coloured ⚠.
        glyph, glyph_style = severity_mark("warning" if downgrade else None, as_ascii=as_ascii)
        count_rows = [
            [tbl, fmt_count(src_counts.get(tbl, 0)), fmt_count(tgt_counts.get(tbl, 0))]
            for tbl in dict.fromkeys(list(src_counts) + list(tgt_counts))
        ]
        print_heading("[Dry run] Restore preview")
        print_definitions([
            ("Source", str(source)),
            ("Target", str(db)),
            ("Schema version", [(glyph, glyph_style), (f" {schema}", None)]),
        ])
        print()
        # The table's own header (table | source | target) is self-describing —
        # no separate "row counts" heading.
        if count_rows:
            print_table(["table", "source", "target"], count_rows)
        else:
            print("  (no tables to compare)")
        return 0

    if db.exists() and not args.force:
        status.error(f"Database already exists: {db}", hint="Use --force to overwrite.")
        return 1

    db.parent.mkdir(parents=True, exist_ok=True)
    for artifact in _database_artifacts(db):
        if artifact.exists():
            artifact.unlink()
    shutil.copy2(source, db)
    size = db.stat().st_size
    status.confirm(f"Restored to: {db} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_slice(args) -> int:
    """Export a filtered subset of conversations into a standalone SQLite database."""
    from siftd.api.slice import slice_database
    from siftd.cli._filters import extract_filter_args

    db = resolve_db(args)
    if not db.exists():
        status.db_missing(db)
        return 1

    target = Path(args.output)
    if target.exists() and not args.force:
        status.error(f"Target already exists: {target}", hint="Use --force to overwrite.")
        return 1

    if target.exists():
        target.unlink()

    filters = extract_filter_args(args)
    rebuild_fts = not args.no_fts

    try:
        result = slice_database(
            source_db=db,
            target_path=target,
            workspace=filters.workspace,
            model=filters.model,
            since=filters.since,
            before=filters.before,
            tag=filters.tag,
            all_tags=filters.all_tags,
            no_tag=filters.no_tag,
            tag_kind=filters.tag_kind,
            tool=filters.tool,
            tool_tag=filters.tool_tag,
            search=filters.search,
            owner=filters.owner,
            rebuild_fts=rebuild_fts,
        )
    except FileNotFoundError as e:
        status.error(str(e))
        return 1

    count = result["conversations"]
    size = result["size_bytes"]
    status.confirm(f"Sliced {count} conversation(s) to: {target} ({size / 1024:.1f} KB)")
    return 0


def cmd_db_merge(args) -> int:
    """Merge an external database (slice) into the main database."""
    source = Path(args.input)

    if not source.exists():
        status.error(f"Source file not found: {source}")
        return 1

    # Validate SQLite magic bytes
    with open(source, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3\x00"):
        status.error(f"Not a valid SQLite database: {source}")
        return 1

    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    from siftd.api.merge import merge_database

    rebuild_fts = not args.no_fts
    dry_run = args.dry_run
    replace = not args.no_replace

    try:
        result = merge_database(
            target_db=db,
            source_path=source,
            rebuild_fts=rebuild_fts,
            dry_run=dry_run,
            replace=replace,
            preflight=not args.no_preflight,
        )
    except (RuntimeError, SiftdError) as e:
        # SiftdError: PreflightError/SchemaUpgradeRequiredError shed their
        # RuntimeError base; catch locally to keep the "Merge failed:" context
        # the backstop wouldn't add.
        status.error(f"Merge failed: {e}")
        return 1

    prefix = "[Dry run] " if dry_run else ""
    status.confirm(f"{prefix}Merged from: {source}")
    conv_parts = [f"{result['conversations']} new"]
    if result["replaced_conversations"]:
        conv_parts.append(f"{result['replaced_conversations']} replaced")
    conv_parts.append(f"{result['skipped_conversations']} skipped")
    fields = [
        ("Conversations", ", ".join(conv_parts)),
        ("Content blobs", str(result["content_blobs"])),
    ]
    if result["tags"]:
        fields.append(("Tags", f"{result['tags']} new"))
    if result["workspaces_matched"]:
        fields.append(("Workspaces", f"{result['workspaces_matched']} matched by git remote"))
    from siftd.output.listing import print_definitions

    print_definitions(fields)
    return 0


def cmd_db_receive(args) -> int:
    """Receive a database from stdin and create-or-merge into the local database.

    Designed to be called via SSH pipe:
        ssh host siftd --db /path db receive < slice.db
    """
    db = resolve_db(args)

    if sys.stdin.isatty():
        print(
            json.dumps({"error": "No data on stdin. Pipe a database file."}),
            file=sys.stderr,
        )
        return 1

    from siftd.api.sync import SYNC_PROTOCOL_VERSION, parse_sync_header

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="siftd-receive-", suffix=".db", delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            # Read enough bytes to detect the protocol header.
            preamble = sys.stdin.buffer.read(8)
            version = parse_sync_header(preamble)
            if version is not None:
                if version > SYNC_PROTOCOL_VERSION:
                    print(
                        json.dumps({
                            "error": (
                                f"Incompatible sync protocol version {version}, "
                                f"max supported is {SYNC_PROTOCOL_VERSION}. "
                                "Upgrade this host."
                            ),
                            "error_type": "protocol_mismatch",
                        }),
                        file=sys.stderr,
                    )
                    return 1
                # Strip header, write remaining data.
                shutil.copyfileobj(sys.stdin.buffer, tmp)
            else:
                # No header (old sender) — write the preamble we already
                # read, then stream the rest.
                tmp.write(preamble)
                shutil.copyfileobj(sys.stdin.buffer, tmp)

        if tmp_path.stat().st_size == 0:
            print(
                json.dumps({"error": "Empty input on stdin."}),
                file=sys.stderr,
            )
            return 1

        if getattr(args, "dry_run", False):
            from siftd.api import open_database
            from siftd.api.database import PreflightError, run_preflight

            try:
                run_preflight(tmp_path)
            except PreflightError as exc:
                print(json.dumps({"error": str(exc)}), file=sys.stderr)
                return 1

            conn = open_database(tmp_path, read_only=True, auto_upgrade=False)
            try:
                src_counts = _table_row_counts(conn)
            finally:
                conn.close()

            tgt_counts: dict[str, int] = {}
            if db.exists():
                target_conn = open_database(db, read_only=True, auto_upgrade=False)
                try:
                    tgt_counts = _table_row_counts(target_conn)
                finally:
                    target_conn.close()

            from siftd.output.common import prefers_ascii
            from siftd.output.listing import print_definitions, print_heading
            from siftd.output.status import severity_mark
            from siftd.output.table import print_table

            as_ascii = prefers_ascii()
            ok_glyph, ok_style = severity_mark(None, as_ascii=as_ascii)  # all-clear ✓
            target_state = "would create new DB" if not db.exists() else "would merge into existing DB"
            count_rows = [
                [tbl, fmt_count(src_counts.get(tbl, 0)), fmt_count(tgt_counts.get(tbl, 0))]
                for tbl in dict.fromkeys(list(src_counts) + list(tgt_counts))
            ]
            print_heading("[Dry run] Receive preview")
            print_definitions([
                ("Target", target_state),
                ("Preflight", [(ok_glyph, ok_style), (" ok", None)]),
            ])
            print()
            # The table's own header (table | incoming | target) is self-describing.
            if count_rows:
                print_table(["table", "incoming", "target"], count_rows)
            else:
                print("  (no tables to compare)")
            return 0

        if getattr(args, "stage", False):
            from siftd.api.inbox import stage_payload

            if getattr(args, "no_preflight", False):
                print(
                    "Note: --no-preflight is ignored when staging; "
                    "preflight runs at process time.",
                    file=sys.stderr,
                )
            result = stage_payload(tmp_path, db)
            print(json.dumps(result))
            return 0

        from siftd.api.receive import receive_database

        rebuild_fts = not args.no_fts
        result = receive_database(
            tmp_path, db, rebuild_fts=rebuild_fts, preflight=not args.no_preflight
        )
        print(json.dumps(result))
        return 0

    except (ValueError, RuntimeError, SiftdError) as e:
        # SiftdError keeps taxonomy members that shed their built-in bases
        # (PreflightError, SchemaUpgradeRequiredError) inside the JSON error
        # envelope this wire-facing command owes machine consumers — the
        # human-format backstop in main() must not see them.
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    except sqlite3.OperationalError as e:
        msg = str(e)
        error_type = "database_locked" if "locked" in msg else "sqlite_error"
        print(
            json.dumps({"error": msg, "error_type": error_type}),
            file=sys.stderr,
        )
        return 1
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def cmd_db_process(args) -> int:
    """Merge all staged inbox payloads into the database."""
    from siftd.api.inbox import process_inbox

    db = resolve_db(args)
    results = process_inbox(db)

    if not results:
        status.info("No staged payloads to process.")
        return 0

    # A severity-bearing result log: each payload's outcome carries its severity
    # glyph (merged ✓ / skipped ℹ / error ✗). The log IS the command's answer, so
    # it rides stdout whole — info/error override their default stderr stream so a
    # `db process > log` redirect captures every outcome, not just the merges.
    errors = 0
    for r in results:
        if r["status"] == "done":
            status.confirm(
                f"{r['id']}: merged ({r.get('conversations', 0)} conversations)"
            )
        elif r["status"] == "skipped":
            status.info(f"{r['id']}: skipped (claimed by another processor)", stream=sys.stdout)
        else:
            status.error(f"{r['id']}: {r.get('error', 'unknown')}", stream=sys.stdout)
            errors += 1

    summary = f"Processed {len(results)} payload(s), {errors} error(s)."
    if errors:
        status.warning(summary, stream=sys.stdout)
    else:
        status.confirm(summary)
    return 1 if errors else 0


def cmd_db_sync_status(args) -> int:
    """Report sync capabilities and inbox status as JSON."""
    from siftd.api.inbox import get_inbox_status
    from siftd.api.sync import SYNC_CAPABILITIES, SYNC_PROTOCOL_VERSION
    from siftd.config import get_config, parse_size_bytes

    db = resolve_db(args)
    inbox = get_inbox_status(db)
    max_body_size = parse_size_bytes(
        str(get_config("serve.request_max_body_size") or "500MB")
    )

    status = {
        "capabilities": sorted(SYNC_CAPABILITIES),
        "inbox": inbox,
        "max_body_size": max_body_size,
        "protocol_version": SYNC_PROTOCOL_VERSION,
    }
    print(json.dumps(status))
    return 0


def cmd_db_send(args) -> int:
    """Slice the local database and write binary SQLite to stdout.

    Metadata (conversation count, size) goes to stderr as JSON.
    Designed for SSH pipe usage — the inverse of ``db receive``.
    """
    from siftd.api.slice import slice_database
    from siftd.api.sync import SYNC_HEADER

    db = resolve_db(args)
    if not db.exists():
        print(
            json.dumps({"error": f"Database not found: {db}"}),
            file=sys.stderr,
        )
        return 1

    if sys.stdout.isatty():
        print(
            json.dumps({"error": "stdout is a terminal. Pipe to a file or command."}),
            file=sys.stderr,
        )
        return 1

    rebuild_fts = not args.no_fts
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="siftd-send-", suffix=".db", delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)

        result = slice_database(
            source_db=db,
            target_path=tmp_path,
            since=getattr(args, "since", None),
            workspace=getattr(args, "workspace", None),
            tag=getattr(args, "tag", None),
            no_tag=getattr(args, "no_tag", None),
            owner=getattr(args, "owner", None),
            rebuild_fts=rebuild_fts,
        )

        conversations = result["conversations"]
        if conversations == 0:
            print(
                json.dumps({"conversations": 0, "size_bytes": 0}),
                file=sys.stderr,
            )
            return 0

        size_bytes = tmp_path.stat().st_size
        sys.stdout.buffer.write(SYNC_HEADER)
        with open(tmp_path, "rb") as f:
            shutil.copyfileobj(f, sys.stdout.buffer)

        print(
            json.dumps({"conversations": conversations, "size_bytes": size_bytes}),
            file=sys.stderr,
        )
        return 0

    except FileNotFoundError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def cmd_db_pull(args) -> int:
    """Pull conversations from a sync remote."""
    from siftd.api.sync import SyncError, SyncRemote, sync_pull
    from siftd.config_sync import get_sync_remote

    remote_cfg = get_sync_remote(args.name)
    if remote_cfg is None:
        status.error(
            f"Remote '{args.name}' not found.",
            hint="Run 'siftd db remote list' to see configured remotes.",
        )
        return 1

    remote = SyncRemote.from_config(remote_cfg)
    cli_strategy = getattr(args, "strategy", None)
    if cli_strategy is not None:
        remote.strategy = cli_strategy

    db = resolve_db(args)

    dry_run = getattr(args, "dry_run", False)
    location = f"{remote.host}:{remote.path}" if remote.host else remote.path

    if not dry_run:
        print(f"Pulling from {args.name} ({location})...", file=sys.stderr)

    # Live transfer bar only on a real run (dry-run just queries the remote).
    live, on_progress = _transfer_progress(enabled=not dry_run)
    try:
        with live:
            result = sync_pull(
                db_path=db,
                remote=remote,
                since=getattr(args, "since", None),
                pull_all=getattr(args, "pull_all", False),
                workspace=getattr(args, "workspace", None),
                tag=getattr(args, "tag", None),
                no_tag=getattr(args, "no_tag", None),
                owner=getattr(args, "owner", None),
                dry_run=dry_run,
                on_progress=on_progress,
            )
    except SyncError as e:
        status.error(f"Pull failed: {e}")
        return 1

    if result.conversations == 0:
        status.confirm(f"Nothing new to pull from {args.name}.")
        return 0

    size_kb = result.size_bytes / 1024

    if result.dry_run:
        status.confirm(f"Would pull {result.conversations} conversations from {args.name} ({size_kb:.1f} KB)")
    else:
        status.confirm(f"Pulled {result.conversations} conversations ({size_kb:.1f} KB)")
    return 0


def cmd_db_remote_add(args) -> int:
    """Register a sync remote."""
    from siftd.config_sync import set_sync_remote

    name = args.name
    target = args.target

    # Parse host:path vs local path
    # Heuristic: if it contains ':' and the part before ':' doesn't look like
    # a drive letter (Windows) or start with '/', treat as host:path.
    if ":" in target and not target.startswith("/"):
        parts = target.split(":", 1)
        host = parts[0]
        path = parts[1]
    else:
        host = None
        path = target

    set_sync_remote(name, host, path)

    if host:
        status.confirm(f"Added remote '{name}': {host}:{path}")
    else:
        status.confirm(f"Added remote '{name}': {path} (local)")
    return 0


def cmd_db_remote_list(args) -> int:
    """List sync remotes."""
    from siftd.config_sync import get_sync_remotes

    remotes = get_sync_remotes()
    if not remotes:
        status.info(
            "No remotes configured.",
            hint="Add one with: siftd db remote add <name> <host:path>",
        )
        return 0

    # name → location listing (wcwidth-correct alignment, replacing the ad-hoc
    # ``:20s`` pad that misaligned on CJK); per-remote last-push/pull ride keyless
    # sub-rows under each remote.
    pairs: list[tuple[str, str]] = []
    for r in remotes:
        location = f"{r['host']}:{r['path']}" if r["host"] else f"{r['path']} (local)"
        pairs.append((r["name"], location))
        if r["last_push"]:
            pairs.append(("", f"last push: {r['last_push']}"))
        if r.get("last_pull"):
            pairs.append(("", f"last pull: {r['last_pull']}"))

    from siftd.output.listing import print_definitions

    print_definitions(pairs)
    return 0


def cmd_db_remote_remove(args) -> int:
    """Unregister a sync remote."""
    from siftd.config_sync import remove_sync_remote

    if remove_sync_remote(args.name):
        status.confirm(f"Removed remote '{args.name}'.")
        return 0
    else:
        status.error(f"Remote '{args.name}' not found.")
        return 1


def _transfer_progress(enabled: bool):
    """Return ``(context, on_progress)`` for a push/pull transfer bar.

    These are human commands (live bar OK), but ``cmd_db_push``/``cmd_db_pull``
    write their status to **stderr** (the preamble + error/warning callouts) and
    reserve stdout for the final success line / any piping. So the bar rides
    stderr too — it sits with the preamble and never collides with stdout.

    ``enabled`` is False for a dry-run (no per-window work) or any non-bar caller:
    then the context is a no-op and ``on_progress`` is None, so sync runs bare.
    """
    import contextlib

    from siftd.output.live import LiveRegion
    from siftd.output.progress_view import ProgressConsumer

    if enabled:
        consumer = ProgressConsumer(shape="bars", live=LiveRegion(stream=sys.stderr))
        if consumer.active:
            return consumer, consumer.feed
    return contextlib.nullcontext(), None


def cmd_db_push(args) -> int:
    """Push conversations to a sync remote."""
    from siftd.api.sync import SyncError, SyncRemote, sync_push
    from siftd.config_sync import get_sync_remote

    remote_cfg = get_sync_remote(args.name)
    if remote_cfg is None:
        status.error(
            f"Remote '{args.name}' not found.",
            hint="Run 'siftd db remote list' to see configured remotes.",
        )
        return 1

    remote = SyncRemote.from_config(remote_cfg)
    cli_strategy = getattr(args, "strategy", None)
    if cli_strategy is not None:
        remote.strategy = cli_strategy

    db = resolve_db(args)
    if not db.exists():
        status.db_missing(db)
        return 1

    dry_run = getattr(args, "dry_run", False)
    location = f"{remote.host}:{remote.path}" if remote.host else remote.path

    if not dry_run:
        print(f"Pushing to {args.name} ({location})...", file=sys.stderr)

    # Live transfer bar only on a real run (a dry-run does no per-window work).
    live, on_progress = _transfer_progress(enabled=not dry_run)
    try:
        with live:
            result = sync_push(
                db_path=db,
                remote=remote,
                since=getattr(args, "since", None),
                push_all=getattr(args, "push_all", False),
                workspace=getattr(args, "workspace", None),
                tag=getattr(args, "tag", None),
                no_tag=getattr(args, "no_tag", None),
                owner=getattr(args, "owner", None),
                dry_run=dry_run,
                on_progress=on_progress,
            )
    except SyncError as e:
        status.error(f"Push failed: {e}")
        return 1
    except FileNotFoundError as e:
        status.error(str(e))
        return 1

    if result.conversations == 0:
        status.confirm(f"Nothing new to push to {args.name}.")
        return 0

    size_kb = result.size_bytes / 1024
    suffix = " (new remote database)" if not result.remote_existed else ""

    if result.dry_run:
        window_hint = f" in {result.windows} windows" if result.windows > 1 else ""
        status.confirm(f"Would push {result.conversations} conversations to {args.name} ({size_kb:.1f} KB){window_hint}")
    else:
        window_hint = f" ({result.windows} windows)" if result.windows > 1 else ""
        # Server-stamped ownership count: only authenticated HTTP pushes to a
        # server that reports it set this — None (older server, no auth,
        # local/SSH transport) omits the suffix rather than showing a fake 0.
        owned_hint = f", {result.owned} owned" if result.owned is not None else ""
        status.confirm(f"Pushed {result.conversations} conversations ({size_kb:.1f} KB{owned_hint}){suffix}{window_hint}")
        if result.windows > 1 and not result.last_push_updated:
            status.warning(
                "Partial push — some windows may not have completed.",
                hint=f"Re-run 'siftd db push {args.name}' to resume from the last successful window.",
            )
    return 0


def build_db_parser(subparsers) -> None:
    """Add the 'db' subparser with nested subcommands."""
    p_db = subparsers.add_parser(
        "db",
        help="Database operations (info, schema-version, backup, restore, vacuum, slice, merge, send, receive, remote, push, pull)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Container-level operations on the siftd database.

Inspection:
  siftd db info                          # database file metadata
  siftd db schema-version                # migration triage info
  siftd db stats                         # full statistics
  siftd db workspaces                    # list workspaces
  siftd db path                          # show XDG paths
  siftd db sync-status                   # sync capabilities and inbox state

Maintenance:
  siftd db vacuum                        # compact database
  siftd db backup /tmp/siftd.db          # online backup
  siftd db restore /tmp/siftd.db         # restore from backup

Sync:
  siftd db slice out.db -w project       # export filtered subset
  siftd db merge laptop-slice.db         # merge slice into main DB
  siftd db send > slice.db               # send via stdout (SSH pipe)
  siftd db push alcove                   # push delta to remote
  siftd db pull alcove                   # pull delta from remote

Sync remotes:
  siftd db remote add alcove host:path   # register sync remote
  siftd db receive < slice.db            # receive via stdin (SSH pipe)
  siftd db process                       # process staged inbox payloads""",
    )
    db_sub = p_db.add_subparsers(dest="db_command")

    # info
    p_info = db_sub.add_parser("info", help="Show database file metadata and schema info")
    p_info.set_defaults(func=cmd_db_info)

    # schema-version
    p_schema_version = db_sub.add_parser(
        "schema-version",
        help="Show migration triage info: current version, target, pending migrations",
    )
    p_schema_version.add_argument("--json", action="store_true", help="Output as JSON")
    p_schema_version.set_defaults(func=cmd_db_schema_version)

    # stats (absorbs status)
    p_stats = db_sub.add_parser("stats", help="Show database statistics")
    p_stats.add_argument("--json", action="store_true", help="Output as JSON")
    p_stats.set_defaults(func=cmd_db_stats)

    # workspaces (absorbs workspaces)
    p_workspaces = db_sub.add_parser("workspaces", help="List workspaces with conversation counts")
    p_workspaces.add_argument("--json", action="store_true", help="Output as JSON")
    p_workspaces.add_argument("-n", "--limit", type=int, default=0, help="Max workspaces (0 = all)")
    p_workspaces.set_defaults(func=cmd_db_workspaces)

    # path (absorbs path)
    p_path = db_sub.add_parser("path", help="Show XDG paths")
    p_path.set_defaults(func=cmd_db_path)

    # vacuum
    p_vacuum = db_sub.add_parser("vacuum", help="Compact database and optimize indexes")
    p_vacuum.set_defaults(func=cmd_db_vacuum)

    # backup
    p_backup = db_sub.add_parser("backup", help="Create a consistent online backup")
    p_backup.add_argument("output", help="Output file path")
    p_backup.add_argument("--force", action="store_true", help="Overwrite existing file")
    p_backup.set_defaults(func=cmd_db_backup)

    # restore
    p_restore = db_sub.add_parser("restore", help="Restore database from a backup file")
    p_restore.add_argument("input", help="Backup file path")
    p_restore.add_argument("--force", action="store_true", help="Overwrite existing database")
    p_restore.add_argument("--dry-run", action="store_true", help="Preview restore without modifying database")
    p_restore.set_defaults(func=cmd_db_restore)

    # slice
    p_slice = db_sub.add_parser(
        "slice",
        help="Export filtered conversations into a standalone database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db slice out.db                          # copy all conversations
  siftd db slice out.db -w project --since 7d    # filter by workspace and date
  siftd db slice out.db -l research:auth         # filter by tag
  siftd db slice out.db --no-fts                 # skip FTS5 rebuild""",
    )
    p_slice.add_argument("output", help="Output database path")
    p_slice.add_argument("--force", action="store_true", help="Overwrite existing file")
    p_slice.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild in output")

    from siftd.cli._filters import add_filter_args

    add_filter_args(p_slice)
    p_slice.set_defaults(func=cmd_db_slice)

    # merge
    p_merge = db_sub.add_parser(
        "merge",
        help="Merge an external database (slice) into the main database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db merge laptop-slice.db              # merge slice into main DB
  siftd db merge laptop-slice.db --dry-run    # preview what would be merged
  siftd db merge laptop-slice.db --no-fts     # skip FTS5 rebuild""",
    )
    p_merge.add_argument("input", help="Source database path to merge in")
    p_merge.add_argument("--dry-run", action="store_true", help="Preview merge without modifying database")
    p_merge.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild")
    p_merge.add_argument("--no-replace", action="store_true",
                         help="Keep existing conversations instead of replacing with newer versions")
    p_merge.add_argument("--no-preflight", action="store_true",
                         help="Skip structural integrity checks on source database")
    p_merge.set_defaults(func=cmd_db_merge)

    # receive
    p_receive = db_sub.add_parser(
        "receive",
        help="Receive a database from stdin and create-or-merge into the local database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Designed for SSH pipe usage — reads a slice DB from stdin and
creates or merges it into the target database.

examples:
  ssh host siftd --db /path/team.db db receive < slice.db
  ssh host siftd --db /path/team.db db receive --no-fts < slice.db""",
    )
    p_receive.add_argument("--no-fts", action="store_true", help="Skip FTS5 index rebuild")
    p_receive.add_argument("--stage", action="store_true",
                           help="Stage payload in inbox for deferred merge (fast ACK)")
    p_receive.add_argument("--no-preflight", action="store_true",
                           help="Skip structural integrity checks on source database")
    p_receive.add_argument("--dry-run", action="store_true",
                           help="Preview incoming payload without writing to database")
    p_receive.set_defaults(func=cmd_db_receive)

    # process
    p_process = db_sub.add_parser(
        "process",
        help="Merge staged inbox payloads into the database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Process all staged payloads from push operations that used --stage.

examples:
  siftd db process                           # merge all pending payloads""",
    )
    p_process.set_defaults(func=cmd_db_process)

    # sync-status
    p_sync_status = db_sub.add_parser(
        "sync-status",
        help="Report sync capabilities and inbox status (JSON)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Returns JSON with receiver capabilities and inbox state.
Used by push pre-flight to negotiate the protocol.

examples:
  siftd db sync-status
  ssh host siftd --db /path/team.db db sync-status""",
    )
    p_sync_status.set_defaults(func=cmd_db_sync_status)

    # send
    p_send = db_sub.add_parser(
        "send",
        help="Slice the database and write binary SQLite to stdout",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Designed for SSH pipe usage — the inverse of ``db receive``.
Writes a slice DB to stdout and metadata JSON to stderr.

examples:
  siftd db send > slice.db                           # send all conversations
  siftd db send --since 7d > slice.db                # send last 7 days
  siftd db send -w project > slice.db                # filter by workspace
  ssh host siftd --db /path db send --no-fts > /tmp/pull.db""",
    )
    p_send.add_argument("--since", metavar="DATE", type=parse_date,
                        help="Send conversations after this date (YYYY-MM-DD, ISO timestamp, 7d, 1w, yesterday, today)")
    p_send.add_argument("-w", "--workspace", metavar="SUBSTR",
                        help="Filter by workspace path substring")
    p_send.add_argument("--tag", action="append", metavar="TAG",
                        help="Only send conversations with these tags (repeatable)")
    p_send.add_argument("--no-tag", action="append", metavar="TAG",
                        help="Exclude conversations with these tags (repeatable)")
    p_send.add_argument("--owner", metavar="USER",
                        help="Filter by conversation owner")
    p_send.add_argument("--no-fts", action="store_true", default=True,
                        help="Skip FTS5 index rebuild (default: skip)")
    p_send.set_defaults(func=cmd_db_send)

    # remote (sub-namespace)
    p_remote = db_sub.add_parser(
        "remote",
        help="Manage sync remotes (add, list, remove)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db remote add alcove alcove:/data/team.db   # SSH remote
  siftd db remote add nas /mnt/nas/siftd/team.db    # local path
  siftd db remote list                               # show all remotes
  siftd db remote remove alcove                      # unregister""",
    )
    remote_sub = p_remote.add_subparsers(dest="remote_command")

    p_remote_add = remote_sub.add_parser("add", help="Register a sync remote")
    p_remote_add.add_argument("name", help="Remote name")
    p_remote_add.add_argument("target", help="host:path (SSH) or /local/path")
    p_remote_add.set_defaults(func=cmd_db_remote_add)

    p_remote_list = remote_sub.add_parser("list", help="List sync remotes")
    p_remote_list.set_defaults(func=cmd_db_remote_list)

    p_remote_remove = remote_sub.add_parser("remove", help="Unregister a sync remote")
    p_remote_remove.add_argument("name", help="Remote name to remove")
    p_remote_remove.set_defaults(func=cmd_db_remote_remove)

    p_remote.set_defaults(func=lambda args: (p_remote.print_help(), 0)[1])

    # push
    p_push = db_sub.add_parser(
        "push",
        help="Push conversations to a sync remote",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db push alcove                       # push delta since last push
  siftd db push alcove --since 7d            # push last 7 days
  siftd db push alcove --all                 # push everything
  siftd db push alcove --dry-run             # preview what would push
  siftd db push alcove -w project            # filter by workspace""",
    )
    p_push.add_argument("name", help="Remote name to push to")
    p_push.add_argument("--since", metavar="DATE", type=parse_date,
                        help="Push conversations after this date (YYYY-MM-DD, ISO timestamp, 7d, 1w, yesterday, today)")
    p_push.add_argument("--all", action="store_true", dest="push_all",
                        help="Push all conversations (ignore last_push)")
    p_push.add_argument("--dry-run", action="store_true",
                        help="Preview what would be pushed without transferring")
    p_push.add_argument("-w", "--workspace", metavar="SUBSTR",
                        help="Filter by workspace path substring")
    p_push.add_argument("--tag", action="append", metavar="TAG",
                        help="Only push conversations with these tags (repeatable)")
    p_push.add_argument("--no-tag", action="append", metavar="TAG",
                        help="Exclude conversations with these tags (repeatable)")
    p_push.add_argument("--owner", metavar="USER",
                        help="Filter by conversation owner")
    p_push.add_argument("--strategy", choices=["incremental", "full"],
                        help="Override push strategy (default: from config or incremental)")
    p_push.set_defaults(func=cmd_db_push)

    # pull
    p_pull = db_sub.add_parser(
        "pull",
        help="Pull conversations from a sync remote",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd db pull alcove                       # pull delta since last pull
  siftd db pull alcove --since 7d            # pull last 7 days
  siftd db pull alcove --all                 # pull everything
  siftd db pull alcove --dry-run             # preview what would pull
  siftd db pull alcove -w project            # filter by workspace""",
    )
    p_pull.add_argument("name", help="Remote name to pull from")
    p_pull.add_argument("--since", metavar="DATE", type=parse_date,
                        help="Pull conversations after this date (YYYY-MM-DD, ISO timestamp, 7d, 1w, yesterday, today)")
    p_pull.add_argument("--all", action="store_true", dest="pull_all",
                        help="Pull all conversations (ignore last_pull)")
    p_pull.add_argument("--dry-run", action="store_true",
                        help="Preview what would be pulled without merging")
    p_pull.add_argument("-w", "--workspace", metavar="SUBSTR",
                        help="Filter by workspace path substring")
    p_pull.add_argument("--tag", action="append", metavar="TAG",
                        help="Only pull conversations with these tags (repeatable)")
    p_pull.add_argument("--no-tag", action="append", metavar="TAG",
                        help="Exclude conversations with these tags (repeatable)")
    p_pull.add_argument("--owner", metavar="USER",
                        help="Filter by conversation owner")
    p_pull.add_argument("--strategy", choices=["incremental", "full"],
                        help="Override pull strategy (default: from config or incremental)")
    p_pull.set_defaults(func=cmd_db_pull)

    # bare 'siftd db' prints help
    p_db.set_defaults(func=lambda args: (p_db.print_help(), 0)[1])
