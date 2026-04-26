"""CLI handlers for data operations (ingest, backfill, migrate, doctor, copy)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.api import open_database
from siftd.cli._common import resolve_db
from siftd.output import fmt_model, fmt_workspace
from siftd.paths import ensure_dirs

if TYPE_CHECKING:
    from siftd.ingestion import IngestEvent, IngestStats


class _AdapterCounts:
    def __init__(self, total: int | None = None) -> None:
        self.total = total or 0
        self.processed = 0
        self.new = 0
        self.updated = 0
        self.replaced = 0
        self.skipped = 0
        self.error = 0
        self.skip_reasons: dict[str, int] = {}

    def add(self, status: str, reason: str | None) -> None:
        self.processed += 1
        if status == "ingested":
            self.new += 1
        elif status == "updated":
            self.updated += 1
        elif status == "replaced":
            self.replaced += 1
        elif status == "skipped":
            self.skipped += 1
            if reason:
                self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        elif status == "error":
            self.error += 1

    @property
    def updated_total(self) -> int:
        return self.updated + self.replaced


class _IngestTextRenderer:
    SUMMARY_WIDTH = 40
    WORKSPACE_WIDTH = 12
    MODEL_WIDTH = 16
    STATUS_WIDTH = 7
    PROGRESS_WIDTH = 7

    def __init__(self, *, verbose: bool, quiet: bool = False) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self._counts: dict[str, _AdapterCounts] = {}
        self._started: set[str] = set()

    def handle_event(self, event: IngestEvent) -> None:
        counts = self._counts.setdefault(event.adapter, _AdapterCounts(event.total))
        if event.total and counts.total != event.total:
            counts.total = event.total
        counts.add(event.status, event.reason)

        if self.quiet:
            return

        if event.status != "skipped":
            if event.adapter not in self._started:
                total = counts.total or event.total or 0
                print(f"{event.adapter} ({total} files)")
                self._started.add(event.adapter)
            print(self._format_line(event))

        if counts.processed == counts.total and event.adapter in self._started:
            self._print_adapter_done(event.adapter, counts)

    def _print_adapter_done(self, adapter: str, counts: _AdapterCounts) -> None:
        parts = [
            f"new {counts.new}",
            f"updated {counts.updated_total}",
            f"skipped {counts.skipped}",
            f"error {counts.error}",
        ]
        summary = ", ".join(parts)
        if self.verbose and counts.skip_reasons:
            reasons = ", ".join(
                f"{reason} {count}"
                for reason, count in sorted(counts.skip_reasons.items())
            )
            summary += f" ({reasons})"
        print(f"  totals: {summary}")

    def print_summary(self, stats: IngestStats) -> None:
        """Print final ingestion summary."""
        active = [
            (name, counts)
            for name, counts in self._counts.items()
            if counts.new > 0 or counts.updated_total > 0 or counts.error > 0
        ]

        has_content = (
            stats.conversations or stats.prompts
            or stats.responses or stats.tool_calls
        )

        if not active:
            if stats.files_found == 0:
                if not self.quiet:
                    print("\nNo files found.")
            else:
                if not self.quiet:
                    msg = f"\n{stats.files_found} files scanned, all up to date."
                    if self.verbose:
                        all_reasons: dict[str, int] = {}
                        for counts in self._counts.values():
                            for reason, count in counts.skip_reasons.items():
                                all_reasons[reason] = all_reasons.get(reason, 0) + count
                        if all_reasons:
                            parts = ", ".join(
                                f"{reason} {count}"
                                for reason, count in sorted(all_reasons.items())
                            )
                            msg += f" ({parts})"
                    print(msg)
            return

        if self.quiet:
            if has_content:
                print(self._format_totals_line(stats))
            return

        # Per-adapter table
        print()
        name_w = max(len(name) for name, _ in active)
        new_w = max(len(str(c.new)) for _, c in active)
        upd_w = max(len(str(c.updated_total)) for _, c in active)
        skip_w = max(len(str(c.skipped)) for _, c in active)

        for name, c in active:
            line = (
                f"{name:<{name_w}}"
                f"  {c.new:>{new_w}} new"
                f"  {c.updated_total:>{upd_w}} updated"
                f"  {c.skipped:>{skip_w}} skipped"
            )
            if self.verbose and c.skip_reasons:
                reasons = ", ".join(
                    f"{reason} {count}"
                    for reason, count in sorted(c.skip_reasons.items())
                )
                line += f" ({reasons})"
            if c.error:
                line += f"  {c.error} error"
            print(line)

        indent = " " * name_w
        print(f"{indent}  ──")
        print(f"{indent}  {self._format_totals_line(stats)}")

    @staticmethod
    def _format_totals_line(stats: IngestStats) -> str:
        return (
            f"{stats.conversations:,} conversations"
            f"  {stats.prompts:,} prompts"
            f"  {stats.responses:,} responses"
            f"  {stats.tool_calls:,} tool_calls"
        )

    def _format_line(self, event: IngestEvent) -> str:
        progress = (
            f"{event.index}/{event.total}"
            if event.index is not None and event.total is not None
            else "--/--"
        )
        status = self._status_label(event.status)
        workspace = fmt_workspace(event.workspace_path) or "--"
        if event.status == "error":
            summary_text = event.error or "error"
        else:
            summary_text = event.summary or "(no summary)"
        summary_text = " ".join(summary_text.split())
        summary_text = self._fit(summary_text, self.SUMMARY_WIDTH)
        model = fmt_model(event.model) if event.model else "--"
        model = self._fit(model, self.MODEL_WIDTH)
        exchanges = f"{event.exchange_count}x" if event.exchange_count is not None else "--"

        return (
            f"  {progress:<{self.PROGRESS_WIDTH}}  "
            f"{status:<{self.STATUS_WIDTH}}  "
            f"{self._fit(workspace, self.WORKSPACE_WIDTH)}  "
            f"{summary_text}  "
            f"{exchanges:>3}  "
            f"{model}"
        )

    @staticmethod
    def _status_label(status: str) -> str:
        if status == "ingested":
            return "new"
        if status == "replaced":
            return "updated"
        return status

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if len(text) > width:
            if width <= 3:
                text = text[:width]
            else:
                text = text[: width - 3] + "..."
        return text.ljust(width)


class _IngestJsonRenderer:
    def __init__(self) -> None:
        self._counts: dict[str, _AdapterCounts] = {}
        self._started: set[str] = set()

    def handle_db(self, *, db: Path, is_new: bool) -> None:
        self._emit({
            "type": "db",
            "path": str(db),
            "state": "created" if is_new else "existing",
        })

    def handle_event(self, event: IngestEvent) -> None:
        counts = self._counts.setdefault(event.adapter, _AdapterCounts(event.total))
        if event.total and counts.total != event.total:
            counts.total = event.total
        counts.add(event.status, event.reason)

        if event.adapter not in self._started:
            self._emit({
                "type": "adapter_start",
                "adapter": event.adapter,
                "total": counts.total,
            })
            self._started.add(event.adapter)

        payload = {
            "type": "file",
            "adapter": event.adapter,
            "status": event.status,
            "reason": event.reason,
            "path": event.path,
            "basename": Path(event.path).name,
            "index": event.index,
            "total": event.total,
        }
        if event.status != "skipped":
            payload.update({
                "workspace": event.workspace_path,
                "summary": event.summary,
                "exchanges": event.exchange_count,
                "model": event.model,
                "error": event.error,
            })
        self._emit(payload)

        if counts.processed == counts.total:
            self._emit({
                "type": "adapter_summary",
                "adapter": event.adapter,
                "total": counts.total,
                "new": counts.new,
                "updated": counts.updated_total,
                "skipped": counts.skipped,
                "error": counts.error,
            })

    def handle_summary(self, stats: IngestStats) -> None:
        self._emit({
            "type": "summary",
            "files": {
                "found": stats.files_found,
                "ingested": stats.files_ingested,
                "replaced": stats.files_replaced,
                "skipped": stats.files_skipped,
                "errored": stats.files_errored,
            },
            "conversations": stats.conversations,
            "prompts": stats.prompts,
            "responses": stats.responses,
            "tool_calls": stats.tool_calls,
            "by_harness": stats.by_harness,
        })

    @staticmethod
    def _emit(payload: dict) -> None:
        print(json.dumps(payload))


def cmd_ingest(args) -> int:
    """Run ingestion from all adapters."""
    from siftd.api import AdapterSelectionError, run_ingest, run_rebuild_fts

    ensure_dirs()

    db = resolve_db(args)
    is_new = not db.exists()
    json_mode = getattr(args, "json", False)

    quiet = getattr(args, "quiet", False)

    if json_mode:
        renderer: _IngestJsonRenderer | _IngestTextRenderer = _IngestJsonRenderer()
        renderer.handle_db(db=db, is_new=is_new)
    else:
        renderer = _IngestTextRenderer(verbose=args.verbose, quiet=quiet)
        if not quiet:
            if is_new:
                print(f"Creating database: {db}")
            else:
                print(f"Using database: {db}")

    # Handle --rebuild-fts flag
    if args.rebuild_fts:
        if json_mode:
            renderer._emit({"type": "fts_rebuild", "status": "start"})
            run_rebuild_fts(db_path=db)
            renderer._emit({"type": "fts_rebuild", "status": "done"})
        else:
            print("Rebuilding FTS index...")
            run_rebuild_fts(db_path=db)
            print("FTS index rebuilt.")
        return 0

    if args.path:
        if json_mode:
            renderer._emit({"type": "scan_paths", "paths": args.path})
        else:
            print(f"Scanning: {', '.join(args.path)}")

    if not json_mode:
        if not quiet and not sys.stdout.isatty():
            print("Tip: use --json for newline-delimited JSON output.", file=sys.stderr)
        if not quiet:
            print("\nIngesting...")
    try:
        result = run_ingest(
            db_path=db,
            adapter_names=args.adapter,
            scan_paths=args.path,
            on_event=renderer.handle_event,
        )
    except AdapterSelectionError as exc:
        message = str(exc)
        if json_mode:
            renderer._emit({"type": "error", "message": message})
        else:
            print(message)
        return 1

    stats = result.stats
    if stats is None:
        return 0
    if json_mode:
        renderer.handle_summary(stats)
    else:
        renderer.print_summary(stats)

    return 0


def cmd_backfill(args) -> int:
    """Backfill derived data from existing records."""
    from siftd.api import run_backfill

    db = resolve_db(args)

    # Warn about --dry-run without --filter-binary
    if getattr(args, "dry_run", False) and not getattr(args, "filter_binary", False):
        print("Note: --dry-run ignored without --filter-binary", file=sys.stderr)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    if args.shell_tags:
        print("Backfilling shell command tags...")
        result = run_backfill(db_path=db, operation="shell_tags")
        counts = result.shell_tag_counts
        total = sum(counts.values())
        if counts:
            print(f"Tagged {total} tool calls:")
            for category, count in sorted(counts.items(), key=lambda x: -x[1]):
                print(f"  shell:{category}: {count}")
        else:
            print("No untagged shell commands found.")
    elif args.derivative_tags:
        print("Backfilling derivative conversation tags...")
        result = run_backfill(db_path=db, operation="derivative_tags")
        count = result.tagged_conversations
        if count:
            print(f"Tagged {count} conversations as siftd:derivative.")
        else:
            print("No untagged derivative conversations found.")
    elif args.filter_binary:
        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            print("Scanning for binary content (dry run)...")
        else:
            print("Filtering binary content from existing blobs...")
        result = run_backfill(db_path=db, operation="filter_binary", dry_run=dry_run)
        print(f"  Filtered: {result.filtered}")
        print(f"  Skipped (no change): {result.skipped}")
        if result.errors:
            print(f"  Errors: {result.errors}")
        if dry_run and result.filtered:
            print("\nRun without --dry-run to apply changes.")
    else:
        # Default: backfill response attributes (original behavior)
        print("Backfilling response attributes (cache tokens)...")
        result = run_backfill(db_path=db, operation="response_attributes")
        count = result.inserted_attributes
        print(f"Done. Inserted {count} attributes.")

    return 0


def cmd_migrate(args) -> int:
    """Run data migrations."""
    from siftd.api.migrations import (
        backfill_git_remotes,
        merge_duplicate_workspaces,
        verify_workspace_identity,
    )

    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    if args.merge_workspaces:
        # Step 1: Backfill git remotes
        print("Step 1: Backfilling git remote URLs for existing workspaces...")

        def on_backfill_progress(msg):
            if args.verbose:
                print(msg)

        stats = backfill_git_remotes(conn, on_progress=on_backfill_progress, dry_run=args.dry_run)
        print(f"  Checked: {stats['checked']}")
        print(f"  Updated: {stats['updated']}")
        print(f"  Skipped (path missing): {stats['skipped_missing']}")
        print(f"  Skipped (no git remote): {stats['skipped_no_git']}")

        # Step 2: Find and optionally merge duplicates
        print("\nStep 2: Finding duplicate workspaces...")
        status = verify_workspace_identity(conn)

        if status["duplicate_groups"] == 0:
            print("  No duplicate workspaces found.")
            conn.close()
            return 0

        print(
            f"  Found {status['duplicate_groups']} groups with {status['duplicate_workspaces']} workspaces sharing git remotes."
        )

        if args.dry_run:
            print("\n[Dry run] Would merge the following workspaces:")

        def on_merge_progress(msg):
            print(msg)

        merge_stats = merge_duplicate_workspaces(
            conn, on_progress=on_merge_progress, dry_run=args.dry_run
        )

        if args.dry_run:
            print(f"\n[Dry run] Would merge {merge_stats['workspaces_merged']} workspaces.")
            print("Run without --dry-run to apply changes.")
        else:
            print(f"\nMerged {merge_stats['workspaces_merged']} workspaces.")
            print(f"Moved {merge_stats['conversations_moved']} conversations.")
    else:
        # Show current status
        status = verify_workspace_identity(conn)
        print("Workspace identity status:")
        print(f"  Total workspaces: {status['total']}")
        print(f"  With git remote: {status['with_remote']}")
        print(f"  Without git remote: {status['without_remote']}")
        if status["duplicate_groups"] > 0:
            print(
                f"  Duplicate groups: {status['duplicate_groups']} ({status['duplicate_workspaces']} workspaces)"
            )
            print("\nRun 'siftd migrate --merge-workspaces' to merge duplicates.")

    conn.close()
    return 0


def cmd_copy(args) -> int:
    """Copy built-in resources to config directory for customization."""
    from siftd.api import (
        CopyError,
        copy_adapter,
        copy_formatter,
        copy_query,
        list_builtin_adapters,
        list_builtin_formatters,
        list_builtin_queries,
    )

    resource_type = args.resource_type
    name = args.name
    force = args.force
    copy_all = args.all

    if resource_type == "adapter":
        if copy_all:
            # Copy all built-in adapters
            names = list_builtin_adapters()
            if not names:
                print("No built-in adapters available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_adapter(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    print(f"Error copying {n}: {e}")
            if copied:
                print("Copied adapters:")
                for n, dest in copied:
                    print(f"  {n} → {dest}")
            return 0

        if not name:
            print("Usage: siftd copy adapter <name> [--force]")
            print("       siftd copy adapter --all [--force]")
            print("\nAvailable adapters:")
            for n in list_builtin_adapters():
                print(f"  {n}")
            return 1

        try:
            dest = copy_adapter(name, force=force)
            print(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            print(f"Error: {e}")
            return 1

    elif resource_type == "query":
        if copy_all:
            names = list_builtin_queries()
            if not names:
                print("No built-in queries available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_query(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    print(f"Error copying {n}: {e}")
            if copied:
                print("Copied queries:")
                for n, dest in copied:
                    print(f"  {n} → {dest}")
            return 0

        if not name:
            available = list_builtin_queries()
            print("Usage: siftd copy query <name> [--force]")
            print("       siftd copy query --all [--force]")
            if available:
                print("\nAvailable queries:")
                for n in available:
                    print(f"  {n}")
            else:
                print("\nNo built-in queries available.")
            return 1

        try:
            dest = copy_query(name, force=force)
            print(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            print(f"Error: {e}")
            return 1

    elif resource_type == "formatter":
        if copy_all:
            names = list_builtin_formatters()
            if not names:
                print("No built-in formatters available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_formatter(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    print(f"Error copying {n}: {e}")
            if copied:
                print("Copied formatters:")
                for n, dest in copied:
                    print(f"  {n} → {dest}")
            return 0

        if not name:
            print("Usage: siftd copy formatter <name> [--force]")
            print("       siftd copy formatter --all [--force]")
            print("\nAvailable formatters:")
            for n in list_builtin_formatters():
                print(f"  {n}")
            return 1

        try:
            dest = copy_formatter(name, force=force)
            print(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            print(f"Error: {e}")
            return 1

    else:
        print(f"Unknown resource type: {resource_type}")
        print("Supported: adapter, query, formatter")
        return 1


def _doctor_fix_pending_tags(args) -> int:
    """Clean up stale sessions and orphaned pending tags."""
    from siftd.api.sessions import cleanup_stale_sessions

    db = resolve_db(args)

    if not db.exists():
        print(f"Database not found: {db}")
        print("Run 'siftd ingest' to create it.")
        return 1

    conn = open_database(db)

    sessions_deleted, tags_deleted = cleanup_stale_sessions(conn, max_age_hours=48, commit=True)

    if args.json:
        out = {
            "sessions_deleted": sessions_deleted,
            "tags_deleted": tags_deleted,
        }
        print(json.dumps(out, indent=2))
    else:
        if sessions_deleted or tags_deleted:
            print(f"Cleaned up {sessions_deleted} stale session(s) and {tags_deleted} orphaned tag(s)")
        else:
            print("No stale sessions or orphaned tags to clean up")

    conn.close()
    return 0


def _doctor_list(args) -> int:
    """List available doctor checks."""
    from siftd.api import list_checks

    checks = list_checks()
    if args.json:
        out = [
            {"name": c.name, "description": c.description, "has_fix": c.has_fix}
            for c in checks
        ]
        print(json.dumps(out, indent=2))
        return 0
    from siftd.output.common import print_table

    rows = [
        [c.name, c.description, "[fix]" if c.has_fix else ""]
        for c in checks
    ]
    print_table(["CHECK", "DESCRIPTION", "FIX"], rows)
    return 0


def _doctor_fix(args) -> int:
    """Execute cached fixes from the last doctor run."""
    from siftd.doctor.fixes import clear_findings_cache, load_findings_cache

    cached = load_findings_cache()
    if not cached:
        print("No pending fixes. Run 'siftd doctor' first to detect issues.", file=sys.stderr)
        return 1

    db = resolve_db(args)
    if not db.exists():
        print(f"Database not found: {db}")
        return 1

    conn = open_database(db)

    # Filter to fixes we know how to execute
    actionable = [entry for entry in cached if entry["fix_command"] in _FIX_REGISTRY]
    if not actionable:
        print("No actionable fixes found in cache.")
        clear_findings_cache()
        conn.close()
        return 0

    print(f"Applying {len(actionable)} fix(es):\n")

    errors = 0
    for entry in actionable:
        cmd = entry["fix_command"]
        label, fn = _FIX_REGISTRY[cmd]
        print(f"  ⠋ {label}...", end="", flush=True)
        try:
            result = fn(conn, db)
            print(f"\r  ✓ {label}: {result}")
        except Exception as e:
            print(f"\r  ✗ {label}: {e}")
            errors += 1

    conn.close()
    clear_findings_cache()

    print()
    if errors:
        print(f"Done with {errors} error(s). Run 'siftd doctor' to verify.")
        return 1
    print("All fixes applied. Run 'siftd doctor' to verify.")
    return 0


def _doctor_fix_flagged(args, fix_command: str) -> int:
    """Run a specific fix directly by fix_command key (flag-discriminated form)."""
    db = resolve_db(args)
    if not db.exists():
        print(f"Database not found: {db}")
        return 1

    conn = open_database(db)
    label, fn = _FIX_REGISTRY[fix_command]
    print(f"  ⠋ {label}...", end="", flush=True)
    try:
        result = fn(conn, db)
        print(f"\r  ✓ {label}: {result}")
        conn.close()
        return 0
    except Exception as e:
        print(f"\r  ✗ {label}: {e}")
        conn.close()
        return 1


# Fix registry: maps fix_command → (label, callable(conn, db_path) → str)
def _fix_ingest(conn, db_path):
    from siftd.api import run_ingest

    stats = run_ingest(db_path=db_path).stats
    if stats is None:
        return "0 file(s) ingested, 0 skipped"
    return f"{stats.files_ingested} file(s) ingested, {stats.files_skipped} skipped"


def _fix_rebuild_fts(conn, db_path):
    from siftd.api.search import rebuild_fts_index

    rebuild_fts_index(conn)
    return "FTS index rebuilt"


def _fix_search_index(conn, db_path):
    from siftd.api.search import build_index

    result = build_index(db_path=db_path, rebuild=False)
    return f"{result.get('chunks_added', 0)} chunk(s) indexed"


def _fix_search_rebuild(conn, db_path):
    from siftd.api.search import build_index

    result = build_index(db_path=db_path, rebuild=True)
    return f"{result.get('chunks_added', 0)} chunk(s) indexed"


def _fix_migrate_blobs(conn, db_path):
    from siftd.api.migrations import migrate_blobs

    result = migrate_blobs(conn)
    return f"{result['migrated']} result(s) migrated, {result['blobs_created']} blob(s) created"


def _fix_backfill_git_remote(conn, db_path):
    from siftd.api.migrations import backfill_git_remotes

    result = backfill_git_remotes(conn)
    return f"{result['updated']} workspace(s) updated"


def _fix_pending_tags(conn, db_path):
    from siftd.api.sessions import cleanup_stale_sessions

    sessions, tags = cleanup_stale_sessions(conn, max_age_hours=48, commit=True)
    return f"{sessions} session(s), {tags} tag(s) cleaned up"


def _fix_blob_refcount(conn, db_path):
    cur = conn.execute("""
        UPDATE content_blobs
        SET ref_count = COALESCE(
            (SELECT COUNT(*) FROM tool_calls WHERE result_hash = content_blobs.hash), 0
        )
    """)
    updated = cur.rowcount
    cur2 = conn.execute("DELETE FROM content_blobs WHERE ref_count = 0")
    deleted = cur2.rowcount
    conn.commit()
    return f"{updated} blob(s) repaired, {deleted} orphan(s) removed"


def _fix_blob_triggers(conn, db_path):
    from siftd.api.database import recreate_blob_triggers

    recreate_blob_triggers(conn)
    conn.commit()
    return "Triggers recreated"


_FIX_REGISTRY = {
    "siftd ingest": ("Ingesting new files", _fix_ingest),
    "siftd ingest --rebuild-fts": ("Rebuilding FTS index", _fix_rebuild_fts),
    "siftd search --index": ("Indexing embeddings", _fix_search_index),
    "siftd search --rebuild": ("Rebuilding embeddings index", _fix_search_rebuild),
    "siftd migrate blobs": ("Migrating tool results to blobs", _fix_migrate_blobs),
    "siftd backfill git-remote": ("Backfilling git remote URLs", _fix_backfill_git_remote),
    "siftd doctor fix --pending-tags": ("Cleaning up stale sessions", _fix_pending_tags),
    "siftd doctor fix --blob-refcount": ("Repairing content blob ref counts", _fix_blob_refcount),
    "siftd doctor fix --triggers": ("Recreating blob ref-count triggers", _fix_blob_triggers),
}


def _doctor_run(args, check_names: list[str] | None = None, show_fixes: bool = False) -> int:
    """Run doctor checks and display findings."""
    db = Path(args.db) if args.db else None
    deep = getattr(args, "deep", False)

    # JSON output — no progress, no painted rendering
    if args.json:
        return _doctor_run_json(args, check_names, show_fixes, db, deep)

    # TTY — painted progress + themed findings
    if sys.stdout.isatty():
        return _doctor_run_painted(args, check_names, show_fixes, db, deep)

    # Non-TTY (piped) — plain text, no progress
    return _doctor_run_plain(args, check_names, show_fixes, db, deep)


def _doctor_run_json(args, check_names, show_fixes, db, deep=False) -> int:
    from siftd.api import run_checks

    try:
        findings = run_checks(checks=check_names or None, db_path=db, deep=deep)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.check))

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    out = {
        "findings": [
            {
                "check": f.check,
                "severity": f.severity,
                "message": f.message,
                "fix_available": f.fix_available,
                "fix_command": f.fix_command,
                "context": f.context,
            }
            for f in findings
        ],
        "summary": {
            "total": len(findings),
            "error": error_count,
            "warning": warning_count,
            "info": sum(1 for f in findings if f.severity == "info"),
        },
    }
    print(json.dumps(out, indent=2))

    # Cache fixable findings for `doctor fix`
    from siftd.doctor.fixes import save_findings_cache

    save_findings_cache(findings)

    fail_count = error_count + warning_count if args.strict else error_count
    return 1 if fail_count > 0 else 0


def _doctor_run_plain(args, check_names, show_fixes, db, deep=False) -> int:
    from siftd.api import run_checks

    try:
        findings = run_checks(checks=check_names or None, db_path=db, deep=deep)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    if not findings:
        print("No issues found.")
        return 0

    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.check))

    icons = {"info": "i", "warning": "!", "error": "x"}
    for finding in findings:
        icon = icons.get(finding.severity, "?")
        print(f"[{icon}] {finding.check}: {finding.message}")
        if finding.fix_command and not show_fixes:
            print(f"    Fix: {finding.fix_command}")

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")
    print()
    print(f"Found {len(findings)} issue(s): {error_count} error, {warning_count} warning, {info_count} info")

    if show_fixes:
        fixable = [f for f in findings if f.fix_available and f.fix_command]
        if fixable:
            print("\nTo fix these issues, run:")
            seen_commands = set()
            for f in fixable:
                if f.fix_command not in seen_commands:
                    print(f"  {f.fix_command}")
                    seen_commands.add(f.fix_command)

    # Cache fixable findings for `doctor fix`
    from siftd.doctor.fixes import save_findings_cache

    save_findings_cache(findings)

    fail_count = error_count + warning_count if args.strict else error_count
    return 1 if fail_count > 0 else 0


def _doctor_run_painted(args, check_names, show_fixes, db, deep=False) -> int:
    from painted import InPlaceRenderer, use_theme

    from siftd.api import list_checks, run_checks
    from siftd.doctor.view import render_progress_block
    from siftd.output.theme import siftd_theme

    all_checks = list_checks()
    if check_names:
        all_checks = [c for c in all_checks if c.name in check_names]
    if not deep:
        all_checks = [c for c in all_checks if getattr(c, "cost", "") != "deep"]
    check_names_ordered = [c.name for c in all_checks]

    completed: dict[str, list] = {}

    with use_theme(siftd_theme), InPlaceRenderer() as renderer:

        def on_done(name, check_findings):
            completed[name] = check_findings
            block = render_progress_block(check_names_ordered, completed, len(completed))
            renderer.render(block)

        # Show initial state
        block = render_progress_block(check_names_ordered, completed, 0)
        renderer.render(block)

        try:
            findings = run_checks(
                checks=check_names or None,
                db_path=db,
                deep=deep,
                on_check_done=on_done,
            )
        except FileNotFoundError as e:
            renderer.finalize()
            print(str(e))
            return 1
        except ValueError as e:
            renderer.finalize()
            print(f"Error: {e}")
            return 1

        # Finalize progress in place — it already shows the full layout
        final_block = render_progress_block(check_names_ordered, completed, len(completed))
        renderer.finalize(final_block)

    # Cache fixable findings for `doctor fix`
    from siftd.doctor.fixes import save_findings_cache

    save_findings_cache(findings)

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    fail_count = error_count + warning_count if args.strict else error_count
    return 1 if fail_count > 0 else 0


def cmd_doctor(args) -> int:
    """Run health checks and report findings."""
    subcommand_args = args.subcommand or []
    action = subcommand_args[0] if subcommand_args else None

    # Warn about --pending-tags without fix subcommand
    if getattr(args, "pending_tags", False) and action != "fix":
        print("Note: --pending-tags ignored without 'fix' subcommand", file=sys.stderr)

    # New subcommands: list, run, fix
    if action == "list":
        return _doctor_list(args)

    if action == "run":
        # doctor run [check1] [check2] ...
        check_names = subcommand_args[1:] if len(subcommand_args) > 1 else None
        return _doctor_run(args, check_names=check_names)

    if action == "fix":
        if getattr(args, "pending_tags", False):
            return _doctor_fix_pending_tags(args)
        if getattr(args, "blob_refcount", False):
            return _doctor_fix_flagged(args, "siftd doctor fix --blob-refcount")
        if getattr(args, "triggers", False):
            return _doctor_fix_flagged(args, "siftd doctor fix --triggers")
        return _doctor_fix(args)

    if action == "db":
        from siftd.doctor.checks import BUILTIN_CHECKS

        _DB_CHECKS = {
            "schema-current", "fts-integrity", "fts-stale",
            "freelist", "blob-migration", "orphaned-chunks",
        }
        db_check_names = [
            c.name for c in BUILTIN_CHECKS
            if c.name.startswith("db-") or c.name in _DB_CHECKS
        ]
        return _doctor_run(args, check_names=db_check_names)

    # Legacy: siftd doctor checks
    if action == "checks":
        return _doctor_list(args)

    # Legacy: siftd doctor fixes
    if action == "fixes":
        return _doctor_run(args, show_fixes=True)

    # Legacy: siftd doctor <check-name> (single check)
    if action:
        return _doctor_run(args, check_names=[action])

    # Default: siftd doctor (run all checks)
    return _doctor_run(args)


def build_data_parser(subparsers) -> None:
    """Add 'ingest', 'backfill', 'migrate', 'doctor', 'copy' subparsers."""
    # ingest
    p_ingest = subparsers.add_parser(
        "ingest",
        help="Ingest logs from all sources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd ingest                      # ingest from all adapters
  siftd ingest -q                   # quiet: totals line only
  siftd ingest -v                   # verbose: per-adapter skip breakdowns
  siftd ingest -a claude_code       # only run claude_code adapter
  siftd ingest -p ~/logs -p /tmp    # scan additional directories
  siftd ingest --rebuild-fts        # rebuild FTS index from scratch""",
    )
    _verbosity = p_ingest.add_mutually_exclusive_group()
    _verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only show totals line",
    )
    _verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show per-adapter skip breakdowns",
    )
    p_ingest.add_argument("-p", "--path", action="append", metavar="DIR", help="Additional directories to scan (can be repeated)")
    p_ingest.add_argument("-a", "--adapter", action="append", metavar="NAME", help="Only run specific adapter(s) (can be repeated)")
    p_ingest.add_argument("--json", action="store_true", help="Output newline-delimited JSON events")
    p_ingest.add_argument("--rebuild-fts", action="store_true", help="Rebuild FTS index from existing data (skips ingestion)")
    p_ingest.set_defaults(func=cmd_ingest)

    # backfill
    p_backfill = subparsers.add_parser(
        "backfill",
        help="Backfill derived data from existing records",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd backfill                    # backfill response attributes (cache tokens)
  siftd backfill --shell-tags       # categorize shell commands as shell:git, shell:test, etc.
  siftd backfill --derivative-tags  # mark siftd-generated conversations
  siftd backfill --filter-binary    # filter binary content from existing blobs
  siftd backfill --filter-binary --dry-run  # preview what would be filtered""",
    )
    p_backfill.add_argument("--shell-tags", action="store_true", help="Tag shell.execute calls with shell:* categories")
    p_backfill.add_argument("--derivative-tags", action="store_true", help="Tag conversations containing siftd search/query as siftd:derivative")
    p_backfill.add_argument("--filter-binary", action="store_true", help="Filter binary content (images, base64) from existing blobs")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview changes without applying (use with --filter-binary)")
    p_backfill.set_defaults(func=cmd_backfill)

    # migrate
    p_migrate = subparsers.add_parser(
        "migrate",
        help="Run data migrations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd migrate                              # show workspace identity status
  siftd migrate --merge-workspaces           # backfill git remotes and merge duplicates
  siftd migrate --merge-workspaces --dry-run # preview what would be merged
  siftd migrate --merge-workspaces -v        # verbose output""",
    )
    p_migrate.add_argument(
        "--merge-workspaces",
        action="store_true",
        help="Backfill git remote URLs and merge duplicate workspaces"
    )
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    p_migrate.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    p_migrate.set_defaults(func=cmd_migrate)

    # copy
    p_copy = subparsers.add_parser(
        "copy",
        help="Copy built-in resources for customization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd copy adapter claude_code    # copy adapter to ~/.config/siftd/adapters/
  siftd copy adapter --all          # copy all built-in adapters
  siftd copy query cost             # copy query to ~/.config/siftd/queries/
  siftd copy formatter markdown     # copy formatter to ~/.config/siftd/formatters/""",
    )
    p_copy.add_argument("resource_type", choices=["adapter", "query", "formatter"], help="Resource type to copy")
    p_copy.add_argument("name", nargs="?", help="Resource name")
    p_copy.add_argument("--all", action="store_true", help="Copy all resources of this type")
    p_copy.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_copy.set_defaults(func=cmd_copy)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Run health checks and maintenance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  siftd doctor                          # run all checks
  siftd doctor list                     # list available checks
  siftd doctor run                      # run all checks (explicit)
  siftd doctor run ingest-pending       # run specific check
  siftd doctor run check1 check2        # run multiple checks
  siftd doctor fix                      # show fix commands for issues
  siftd doctor fix --pending-tags       # clean up stale sessions/tags
  siftd doctor --json                   # output as JSON
  siftd doctor --strict                 # exit 1 on warnings (for CI)

legacy (still supported):
  siftd doctor checks                   # same as 'list'
  siftd doctor fixes                    # same as 'fix'
  siftd doctor ingest-pending           # same as 'run ingest-pending'

exit codes:
  0  no errors (or no warnings with --strict)
  1  errors found (or warnings with --strict)""",
    )
    p_doctor.add_argument("subcommand", nargs="*", help="list | run [checks...] | fix | <check-name>")
    p_doctor.add_argument("--json", action="store_true", help="Output as JSON")
    p_doctor.add_argument("--strict", action="store_true", help="Exit 1 on warnings (not just errors). Useful for CI.")
    p_doctor.add_argument("--pending-tags", action="store_true", help="Clean up stale sessions and orphaned pending tags (use with 'fix')")
    p_doctor.add_argument("--deep", action="store_true", help="Include deep integrity checks (slower).")
    p_doctor.add_argument("--blob-refcount", action="store_true", dest="blob_refcount", help="Re-derive blob ref counts and sweep orphans (use with 'fix').")
    p_doctor.add_argument("--triggers", action="store_true", help="Recreate blob ref-count triggers (use with 'fix').")
    p_doctor.set_defaults(func=cmd_doctor)
