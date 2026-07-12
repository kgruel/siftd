"""CLI handlers for data operations (ingest, backfill, migrate, doctor, copy)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from siftd.api import open_database
from siftd.cli._common import embedding_awaiting_message, resolve_db
from siftd.output import fmt_count, fmt_model, fmt_workspace, status
from siftd.paths import ensure_dirs

if TYPE_CHECKING:
    from siftd.ingestion import IngestEvent, IngestStats
    from siftd.output.progress_view import ProgressConsumer, ProgressEvent


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
        self._consumer: ProgressConsumer | None = None  # attached for the TTY bars

    def attach_consumer(self, consumer: ProgressConsumer) -> None:
        """Attach the progress consumer whose bars replace per-file streaming on a TTY."""
        self._consumer = consumer

    def handle_event(self, event: IngestEvent) -> None:
        counts = self._counts.setdefault(event.adapter, _AdapterCounts(event.total))
        if event.total and counts.total != event.total:
            counts.total = event.total
        counts.add(event.status, event.reason)

        if self.quiet:
            return

        # Live tier: every adapter gets a re-painting progress bar; the per-file
        # scroll is gone. Force a paint past the throttle on a new adapter, an
        # error, or completion — the moments worth not dropping.
        if self._consumer is not None and self._consumer.active:
            new_adapter = event.adapter not in self._started
            self._started.add(event.adapter)
            done = bool(counts.total) and counts.processed >= counts.total
            force = new_adapter or event.status == "error" or done
            self._consumer.feed(self._progress_event(event.adapter, counts, done=done, terminal=force))
            return

        if event.status != "skipped":
            if event.adapter not in self._started:
                total = counts.total or event.total or 0
                print(f"{event.adapter} ({total} files)")
                self._started.add(event.adapter)
            print(self._format_line(event))

        if counts.processed == counts.total and event.adapter in self._started:
            self._print_adapter_done(event.adapter, counts)

    @staticmethod
    def _progress_event(
        adapter: str, counts: _AdapterCounts, *, done: bool, terminal: bool
    ) -> ProgressEvent:
        """Fold one adapter's running counts into a generic ``ProgressEvent``.

        The per-file ``IngestEvent`` stream maps onto the shared bars contract so
        ingest, push and migrate paint through one consumer: the new/upd/skip
        running counts become the amber tally (``err`` joins only when it fires —
        a non-zero error count surfaces as the row's ``✗`` glyph, the count itself
        staying on the uniform metric thread); ``total or None`` lets a zero-total
        adapter draw the indeterminate sweep rather than a ``0/0`` bar.
        """
        from siftd.output.progress_view import ProgressEvent

        tally = {"new": counts.new, "upd": counts.updated_total, "skip": counts.skipped}
        if counts.error:
            tally["err"] = counts.error
        status = "error" if (done and counts.error) else "done" if done else "progress"
        return ProgressEvent(
            group=adapter,
            index=counts.processed,
            total=counts.total or None,
            tally=tally,
            status=status,
            terminal=terminal,
        )

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
        has_content = (
            stats.conversations or stats.prompts
            or stats.responses or stats.tool_calls
        )

        if not has_content:
            # Nothing landed — an empty-state note (the bars already showed the
            # per-adapter skip/err disposition). Don't claim "up to date" if
            # files actually errored.
            if not self.quiet:
                if stats.files_found == 0:
                    status.info("No files found.")
                elif stats.files_errored:
                    status.warning(f"{stats.files_errored} file(s) errored; nothing new ingested.")
                else:
                    detail = self._skip_reasons_detail() if self.verbose else None
                    status.info(f"{stats.files_found} files scanned, all up to date.", detail=detail)
            return

        if self.quiet:
            print(self._format_totals_line(stats))
            return

        # Per-adapter content yield + grand-total footer, deposited below the
        # bars (which already carried the new/upd/skip disposition).
        print()
        self._print_summary_table(stats)

    def _print_summary_table(self, stats: IngestStats) -> None:
        from painted import Align, current_palette, join_vertical, print_block

        from siftd.output.live import text_row
        from siftd.output.table import Col, render_table, table_budget
        from siftd.output.theme import domain_styles

        pal = current_palette()
        ds = domain_styles()
        # Per-adapter content from stats.by_harness — only adapters that actually
        # yielded something (an all-skip adapter contributes no rows here).
        yielded = [
            (name, h)
            for name, h in stats.by_harness.items()
            if h.get("conversations") or h.get("prompts")
            or h.get("responses") or h.get("tool_calls")
        ]
        # The per-adapter counts join the amber metric thread, consistent with the
        # query/peek lists; the ADAPTER name stays plain (the row label) and the
        # totals recap below stays muted (a sentence, not a single headline figure).
        cols = [
            Col("ADAPTER", lambda it: it[0], fill=True, min_width=10),
            Col("CONVERSATIONS", lambda it: str(it[1].get("conversations", 0)),
                align=Align.END, style=ds.metric),
            Col("PROMPTS", lambda it: str(it[1].get("prompts", 0)),
                align=Align.END, style=ds.metric),
            Col("RESPONSES", lambda it: str(it[1].get("responses", 0)),
                align=Align.END, style=ds.metric),
            Col("TOOL_CALLS", lambda it: str(it[1].get("tool_calls", 0)),
                align=Align.END, style=ds.metric),
        ]

        budget, as_ascii = table_budget()
        table_block = render_table(cols, yielded, width=budget, as_ascii=as_ascii)
        totals = text_row([("  " + self._format_totals_line(stats), pal.muted)])
        parts = [table_block, totals]
        if self.verbose:
            detail = self._skip_reasons_detail()
            if detail:
                parts.append(text_row([("  skipped: " + detail, pal.muted)]))
        from siftd.output.common import should_use_ansi

        print_block(join_vertical(*parts), use_ansi=should_use_ansi())

    def _skip_reasons_detail(self) -> str | None:
        """Aggregate skip-reason breakdown across all adapters, or None."""
        all_reasons: dict[str, int] = {}
        for counts in self._counts.values():
            for reason, count in counts.skip_reasons.items():
                all_reasons[reason] = all_reasons.get(reason, 0) + count
        if not all_reasons:
            return None
        return ", ".join(f"{reason} {count}" for reason, count in sorted(all_reasons.items()))

    @staticmethod
    def _format_totals_line(stats: IngestStats) -> str:
        return (
            f"{fmt_count(stats.conversations)} conversations"
            f"  {fmt_count(stats.prompts)} prompts"
            f"  {fmt_count(stats.responses)} responses"
            f"  {fmt_count(stats.tool_calls)} tool_calls"
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

    quiet_explicit = getattr(args, "quiet", None)
    auto_quiet = (
        quiet_explicit is None
        and not args.verbose
        and not sys.stdout.isatty()
    )
    quiet = bool(quiet_explicit) or auto_quiet

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

    if not json_mode and auto_quiet:
        status.info(
            "ingest output quieted (not a TTY). "
            "Pass -v to force verbose output, or -q to silence this hint."
        )

    # Handle --rebuild-fts flag
    if args.rebuild_fts:
        if json_mode:
            renderer._emit({"type": "fts_rebuild", "status": "start"})
            run_rebuild_fts(db_path=db)
            renderer._emit({"type": "fts_rebuild", "status": "done"})
        else:
            if not quiet:
                print("Rebuilding FTS index...")
            run_rebuild_fts(db_path=db)
            if not quiet:
                print("FTS index rebuilt.")
        return 0

    if args.path:
        if json_mode:
            renderer._emit({"type": "scan_paths", "paths": args.path})
        else:
            if not quiet:
                print(f"Scanning: {', '.join(args.path)}")

    if not json_mode:
        if not quiet:
            print("\nIngesting...")

    # On a Unicode TTY, per-adapter progress bars re-paint in place; a pipe /
    # --json / non-UTF-8 locale leaves the consumer inactive and the renderer
    # falls back to its plain streaming (or quiet) path. The consumer owns the
    # live region: ``with consumer`` deposits the final bar frame on a clean exit
    # and restores the cursor on any exit (the same shape push/pull use).
    from siftd.output.live import LiveRegion
    from siftd.output.progress_view import ProgressConsumer

    consumer = ProgressConsumer(shape="bars", live=LiveRegion(enabled=not quiet and not json_mode))
    if not json_mode:
        renderer.attach_consumer(consumer)  # type: ignore[union-attr]

    def _on_egress_notice(text: str) -> None:
        # The remote first-egress disclosure surfaces the moment auto-index decides to send,
        # BEFORE the content leaves — never gated by --quiet (it's a privacy notice).
        if json_mode:
            renderer._emit({"type": "auto_index_notice", "message": text})
        else:
            status.info(text)

    try:
        with consumer:
            result = run_ingest(
                db_path=db,
                adapter_names=args.adapter,
                scan_paths=args.path,
                on_event=renderer.handle_event,
                on_notice=_on_egress_notice,
            )
    except AdapterSelectionError as exc:
        message = str(exc)
        if json_mode:
            renderer._emit({"type": "error", "message": message})
        else:
            status.error(message)
        return 1

    stats = result.stats
    if stats is None:
        return 0
    if json_mode:
        renderer.handle_summary(stats)
    else:
        renderer.print_summary(stats)

    # Adapter health: zero-discovery, non-core file errors, drop-in import failures.
    zero_discovery = sorted(set(result.adapters) - set(stats.by_harness))
    # File errors from non-core adapters are tagged with their tier so expectations
    # are set: core adapters are expected to parse cleanly; contrib is best-effort
    # and frozen may lag upstream format changes.
    tiers = result.adapter_tiers
    noncore_errors = sorted(
        (name, counts.get("errors", 0), tiers.get(name, "contrib"))
        for name, counts in stats.by_harness.items()
        if counts.get("errors", 0) and tiers.get(name, "contrib") != "core"
    )
    if json_mode:
        for name in zero_discovery:
            renderer._emit({
                "type": "adapter_warning",
                "kind": "zero_discovery",
                "adapter": name,
                "message": f"Adapter '{name}' found nothing to ingest — check scan paths or adapter config",
            })
        for name, errors, tier in noncore_errors:
            renderer._emit({
                "type": "adapter_warning",
                "kind": "noncore_errors",
                "adapter": name,
                "tier": tier,
                "errors": errors,
                "message": f"Adapter '{name}' had {errors} file error(s) — {tier}-tier adapter, supported best-effort",
            })
        for path, error in result.dropin_failures:
            renderer._emit({
                "type": "adapter_warning",
                "kind": "failed_import",
                "path": str(path),
                "message": f"Drop-in adapter at '{path}' failed to load: {error}",
            })
    else:
        if not quiet:
            for name in zero_discovery:
                status.info(f"Adapter '{name}' found nothing to ingest — check scan paths or adapter config")
            for name, errors, tier in noncore_errors:
                status.warning(
                    f"Adapter '{name}' had {errors} file error(s) — {tier}-tier adapter, supported best-effort"
                )
        for path, error in result.dropin_failures:
            status.warning(f"Drop-in adapter at '{path}' failed to load: {error}")

    _render_auto_index(result.auto_index, json_mode=json_mode, quiet=quiet, renderer=renderer)
    return 0


def _render_auto_index(report, *, json_mode: bool, quiet: bool, renderer) -> None:
    """Surface the post-ingest auto-index OUTCOME (embedded / awaiting / warning).

    The first-egress notice is NOT rendered here — it is surfaced live, pre-embed, through
    run_ingest's on_notice callback (see cmd_ingest). This runs after run_ingest returns.
    """
    if report is None:
        return
    if json_mode:
        renderer._emit({
            "type": "auto_index",
            "ran": report.ran,
            "chunks_added": report.chunks_added,
            "conversations_indexed": report.conversations_indexed,
            "awaiting": report.awaiting,
            "skipped_reason": report.skipped_reason,
            "notice": report.notice,
            "error": report.error,
        })
        return
    if quiet:
        return
    if report.error:
        status.warning(
            f"Auto-index skipped: {report.error}",
            hint="Run 'siftd embed' to catch up.",
        )
    elif report.skipped_reason:
        subject, hint = embedding_awaiting_message(report.awaiting)
        status.info(subject, hint=hint)
    elif report.ran:
        # Honest at 0 chunks (a stale conversation whose new content all filtered away still
        # counts as embedded — its old chunks were legitimately swept).
        status.info(
            f"Embedded {fmt_count(report.conversations_indexed)} new conversation(s) "
            f"({fmt_count(report.chunks_added)} chunks)."
        )


def cmd_backfill(args) -> int:
    """Backfill derived data from existing records."""
    from siftd.api import run_backfill

    db = resolve_db(args)

    # --dry-run is only meaningful for --filter-binary and --git-remote
    if getattr(args, "dry_run", False) and not (
        getattr(args, "filter_binary", False) or getattr(args, "git_remote", False)
    ):
        status.info("--dry-run ignored without --filter-binary or --git-remote")

    if not db.exists():
        status.db_missing(db)
        return 1

    if args.shell_tags:
        print("Backfilling shell command tags...")
        result = run_backfill(db_path=db, operation="shell_tags")
        counts = result.shell_tag_counts
        total = sum(counts.values())
        if counts:
            from siftd.output.listing import print_definitions, print_heading
            from siftd.output.theme import domain_styles

            ds = domain_styles()
            print_heading(f"Tagged {total} tool calls")
            print_definitions(
                (f"shell:{category}", [(fmt_count(count), ds.metric)])
                for category, count in sorted(counts.items(), key=lambda x: -x[1])
            )
        else:
            status.info("No untagged shell commands found.")
    elif args.derivative_tags:
        print("Backfilling derivative conversation tags...")
        result = run_backfill(db_path=db, operation="derivative_tags")
        count = result.tagged_conversations
        if count:
            status.confirm(f"Tagged {count} conversations as siftd:derivative.")
        else:
            status.info("No untagged derivative conversations found.")
    elif getattr(args, "models", False):
        print("Re-parsing model names to canonical form...")
        result = run_backfill(db_path=db, operation="models")
        if result.updated_models:
            status.confirm(f"Canonicalized {result.updated_models} model name(s).")
            status.info("Prices reproject on next open; cost refreshes on next ingest.")
        else:
            status.info("No model names needed canonicalizing.")
    elif getattr(args, "pricing", False):
        print("Repricing from the pricing reference (reproject + rebuild rollup)...")
        result = run_backfill(db_path=db, operation="pricing")
        status.confirm(f"Repriced {result.repriced_rows} usage rows from the reference.")
    elif args.filter_binary:
        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            print("Scanning for binary content (dry run)...")
        else:
            print("Filtering binary content from existing blobs...")
        result = run_backfill(db_path=db, operation="filter_binary", dry_run=dry_run)
        from siftd.output.listing import print_definitions
        from siftd.output.theme import domain_styles

        ds = domain_styles()
        rows = [
            ("Filtered", [(fmt_count(result.filtered), ds.metric)]),
            ("Skipped (no change)", [(fmt_count(result.skipped), ds.metric)]),
        ]
        if result.errors:
            rows.append(("Errors", [(fmt_count(result.errors), ds.metric)]))
        print_definitions(rows)
        if dry_run and result.filtered:
            status.info("Run without --dry-run to apply changes.")
    elif args.git_remote:
        from siftd.api.migrations import backfill_git_remotes

        dry_run = getattr(args, "dry_run", False)
        conn = open_database(db)
        try:
            if dry_run:
                print("Scanning for workspaces without git remote (dry run)...")
            else:
                print("Backfilling git remote URLs for workspaces missing them...")

            def on_progress(event):
                if getattr(args, "verbose", False) and event.message:
                    print(event.message)

            stats = backfill_git_remotes(conn, on_progress=on_progress, dry_run=dry_run)
            from siftd.output.listing import print_definitions
            from siftd.output.theme import domain_styles

            ds = domain_styles()
            print_definitions([
                ("Checked", [(fmt_count(stats["checked"]), ds.metric)]),
                ("Updated", [(fmt_count(stats["updated"]), ds.metric)]),
                ("Skipped (path missing)", [(fmt_count(stats["skipped_missing"]), ds.metric)]),
                ("Skipped (no git remote)", [(fmt_count(stats["skipped_no_git"]), ds.metric)]),
            ])
            if dry_run and stats["updated"]:
                status.info("Run without --dry-run to apply changes.")
        finally:
            conn.close()
    else:
        # Default: backfill response attributes (original behavior)
        print("Backfilling response attributes (cache tokens)...")
        result = run_backfill(db_path=db, operation="response_attributes")
        count = result.inserted_attributes
        status.confirm(f"Done. Inserted {count} attributes.")

    return 0


def _run_merge_workspaces(conn, *, dry_run: bool, verbose: bool) -> int:
    """The ``migrate --merge-workspaces`` flow: backfill remotes, then merge dups.

    A two-step sequence — the step-log progress shape (sibling of ``doctor
    --fix``). On a Unicode TTY the steps render as a live ``ProgressConsumer``
    step-log (spinner → ``✓``); a pipe / non-Unicode locale keeps the original
    plain "Step 1… / Step 2…" prints. The status callouts that summarise the run
    land *after* the live region closes, so they never tear the live frame.
    """
    from siftd.api.migrations import (
        backfill_git_remotes,
        merge_duplicate_workspaces,
        verify_workspace_identity,
    )
    from siftd.output.progress_view import ProgressConsumer, ProgressEvent

    backfill_group = "backfill git remotes"
    merge_group = "merge workspaces"

    def _backfill_summary(stats: dict) -> str:
        return (
            f"{backfill_group}: {stats['updated']} updated, "
            f"{stats['checked']} checked, "
            f"{stats['skipped_missing'] + stats['skipped_no_git']} skipped"
        )

    consumer = ProgressConsumer(shape="steps")
    if not consumer.active:
        return _run_merge_workspaces_plain(
            conn,
            dry_run=dry_run,
            verbose=verbose,
            backfill=backfill_git_remotes,
            merge=merge_duplicate_workspaces,
            verify=verify_workspace_identity,
            backfill_group=backfill_group,
            merge_group=merge_group,
        )

    # Live step-log. Each step starts as a pending spinner, resolves to ✓ with a
    # summary message; the producers' per-row events update the in-flight line.
    summaries: dict[str, dict] = {}
    with consumer:
        consumer.feed(ProgressEvent(group=backfill_group, status="progress", terminal=True))
        stats = backfill_git_remotes(conn, on_progress=consumer.feed, group=backfill_group, dry_run=dry_run)
        summaries["backfill"] = stats
        consumer.feed(ProgressEvent(
            group=backfill_group, status="done", message=_backfill_summary(stats), terminal=True
        ))

        ws_status = verify_workspace_identity(conn)
        if ws_status["duplicate_groups"] == 0:
            consumer.feed(ProgressEvent(
                group=merge_group, status="skipped",
                message=f"{merge_group}: no duplicates", terminal=True,
            ))
        else:
            consumer.feed(ProgressEvent(
                group=merge_group, status="progress",
                message=f"{merge_group}: {ws_status['duplicate_groups']} group(s)",
                terminal=True,
            ))
            merge_stats = merge_duplicate_workspaces(
                conn, on_progress=consumer.feed, group=merge_group, dry_run=dry_run
            )
            summaries["merge"] = merge_stats
            verb = "would merge" if dry_run else "merged"
            consumer.feed(ProgressEvent(
                group=merge_group, status="done",
                message=f"{merge_group}: {verb} {merge_stats['workspaces_merged']} workspace(s)",
                terminal=True,
            ))

    # Summary callouts, after the region closes (they'd tear the live frame).
    if "merge" not in summaries:
        status.info("No duplicate workspaces found.")
        return 0
    merge_stats = summaries["merge"]
    if dry_run:
        status.confirm(f"[Dry run] Would merge {merge_stats['workspaces_merged']} workspaces.")
        status.info("Run without --dry-run to apply changes.")
    else:
        status.confirm(f"Merged {merge_stats['workspaces_merged']} workspaces.")
        status.confirm(f"Moved {merge_stats['conversations_moved']} conversations.")
    return 0


def _run_merge_workspaces_plain(
    conn, *, dry_run, verbose, backfill, merge, verify, backfill_group, merge_group
) -> int:
    """The non-TTY plain path for ``--merge-workspaces`` (the original prints)."""
    # Step 1: Backfill git remotes
    print("Step 1: Backfilling git remote URLs for existing workspaces...")

    def on_backfill_progress(event: ProgressEvent) -> None:
        if verbose and event.message:
            print(event.message)

    stats = backfill(conn, on_progress=on_backfill_progress, group=backfill_group, dry_run=dry_run)
    print(f"  Checked: {fmt_count(stats['checked'])}")
    print(f"  Updated: {fmt_count(stats['updated'])}")
    print(f"  Skipped (path missing): {fmt_count(stats['skipped_missing'])}")
    print(f"  Skipped (no git remote): {fmt_count(stats['skipped_no_git'])}")

    # Step 2: Find and optionally merge duplicates
    print("\nStep 2: Finding duplicate workspaces...")
    ws_status = verify(conn)

    if ws_status["duplicate_groups"] == 0:
        status.info("No duplicate workspaces found.")
        return 0

    status.info(
        f"Found {ws_status['duplicate_groups']} groups with "
        f"{ws_status['duplicate_workspaces']} workspaces sharing git remotes."
    )

    if dry_run:
        print("\n[Dry run] Would merge the following workspaces:")

    def on_merge_progress(event: ProgressEvent) -> None:
        if event.message:
            print(event.message)

    merge_stats = merge(conn, on_progress=on_merge_progress, group=merge_group, dry_run=dry_run)

    if dry_run:
        status.confirm(f"[Dry run] Would merge {merge_stats['workspaces_merged']} workspaces.")
        status.info("Run without --dry-run to apply changes.")
    else:
        status.confirm(f"Merged {merge_stats['workspaces_merged']} workspaces.")
        status.confirm(f"Moved {merge_stats['conversations_moved']} conversations.")
    return 0


def cmd_migrate(args) -> int:
    """Run data migrations."""
    from siftd.api.migrations import verify_workspace_identity

    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
        return 1

    conn = open_database(db)

    if args.merge_workspaces:
        rc = _run_merge_workspaces(conn, dry_run=args.dry_run, verbose=args.verbose)
        conn.close()
        return rc
    else:
        # Show current status
        from siftd.output.listing import print_definitions, print_heading
        from siftd.output.theme import domain_styles

        ws_status = verify_workspace_identity(conn)
        ds = domain_styles()
        print_heading("Workspace identity status")
        rows = [
            ("Total workspaces", [(fmt_count(ws_status["total"]), ds.metric)]),
            ("With git remote", [(fmt_count(ws_status["with_remote"]), ds.metric)]),
            ("Without git remote", [(fmt_count(ws_status["without_remote"]), ds.metric)]),
        ]
        if ws_status["duplicate_groups"] > 0:
            rows.append((
                "Duplicate groups",
                [
                    (fmt_count(ws_status["duplicate_groups"]), ds.metric),
                    (f" ({ws_status['duplicate_workspaces']} workspaces)", None),
                ],
            ))
        print_definitions(rows)
        if ws_status["duplicate_groups"] > 0:
            status.info("Run 'siftd migrate --merge-workspaces' to merge duplicates.")

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
                status.error("No built-in adapters available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_adapter(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    status.error(f"Could not copy {n}: {e}")
            if copied:
                from siftd.output.listing import print_definitions, print_heading

                print_heading("Copied adapters")
                print_definitions((n, str(dest)) for n, dest in copied)
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
            status.confirm(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            status.error(str(e))
            return 1

    elif resource_type == "query":
        if copy_all:
            names = list_builtin_queries()
            if not names:
                status.error("No built-in queries available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_query(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    status.error(f"Could not copy {n}: {e}")
            if copied:
                from siftd.output.listing import print_definitions, print_heading

                print_heading("Copied queries")
                print_definitions((n, str(dest)) for n, dest in copied)
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
            status.confirm(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            status.error(str(e))
            return 1

    elif resource_type == "formatter":
        if copy_all:
            names = list_builtin_formatters()
            if not names:
                status.error("No built-in formatters available.")
                return 1
            copied = []
            for n in names:
                try:
                    dest = copy_formatter(n, force=force)
                    copied.append((n, dest))
                except CopyError as e:
                    status.error(f"Could not copy {n}: {e}")
            if copied:
                from siftd.output.listing import print_definitions, print_heading

                print_heading("Copied formatters")
                print_definitions((n, str(dest)) for n, dest in copied)
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
            status.confirm(f"Copied {name} → {dest}")
            return 0
        except CopyError as e:
            status.error(str(e))
            return 1

    else:
        status.error(
            f"Unknown resource type: {resource_type}",
            hint="Supported: adapter, query, formatter",
        )
        return 1


def _doctor_fix_pending_tags(args) -> int:
    """Clean up stale sessions and orphaned pending tags."""
    from siftd.api.sessions import cleanup_stale_sessions

    db = resolve_db(args)

    if not db.exists():
        status.db_missing(db)
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
            status.confirm(f"Cleaned up {sessions_deleted} stale session(s) and {tags_deleted} orphaned tag(s)")
        else:
            status.info("No stale sessions or orphaned tags to clean up")

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
    from siftd.output import print_table

    rows = [
        [c.name, c.description, "[fix]" if c.has_fix else ""]
        for c in checks
    ]
    print_table(["CHECK", "DESCRIPTION", "FIX"], rows)
    return 0


def _run_fix_steps(steps: list, conn, db) -> int:
    """Run ``(label, fn)`` fix steps as a live spinner step-log; return error count.

    The two hand-rolled ``\\r``-overwrite spinners dissolve onto the generic
    ``ProgressConsumer(shape="steps")`` — each step feeds a pending spinner that
    resolves in place to ``✓``/``✗`` on a Unicode TTY; a pipe / non-Unicode locale
    prints the resolved lines plainly (ascii-aware), keeping the per-step feedback
    the ``\\r`` form gave. A failing step is reported, not raised.
    """
    from painted import ASCII_ICONS, current_icons

    from siftd.output.common import supports_unicode
    from siftd.output.progress_view import ProgressConsumer, ProgressEvent

    plain_icons = current_icons() if supports_unicode() else ASCII_ICONS
    errors = 0

    consumer = ProgressConsumer(shape="steps")
    with consumer:
        for label, fn in steps:
            # A pending spinner row for the in-flight step; the prior steps' rows
            # stay resolved (the consumer keeps per-group state).
            if consumer.active:
                consumer.feed(ProgressEvent(group=label, message=label, status="progress", terminal=True))
            try:
                result = fn(conn, db)
                outcome, text = "done", f"{label}: {result}"
            except Exception as e:  # noqa: BLE001 — fix failures are reported, not raised
                outcome, text = "error", f"{label}: {e}"
                errors += 1
            if consumer.active:
                # Resolve the row in place (✓/✗) by re-feeding the same group.
                consumer.feed(ProgressEvent(group=label, message=text, status=outcome, terminal=True))
            else:
                mark = plain_icons.ok if outcome == "done" else plain_icons.error
                print(f"  {mark} {text}")
    return errors


def _doctor_fix(args) -> int:
    """Execute cached fixes from the last doctor run."""
    from siftd.doctor.fixes import clear_findings_cache, load_findings_cache

    cached = load_findings_cache()
    if not cached:
        status.error("No pending fixes.", hint="Run 'siftd doctor' first to detect issues.")
        return 1

    db = resolve_db(args)
    if not db.exists():
        status.error(f"Database not found: {db}")
        return 1

    conn = open_database(db)

    # Filter to fixes we know how to execute
    actionable = [entry for entry in cached if entry["fix_command"] in _FIX_REGISTRY]
    if not actionable:
        status.info("No actionable fixes found in cache.")
        clear_findings_cache()
        conn.close()
        return 0

    print(f"Applying {fmt_count(len(actionable))} fix(es):\n")

    steps = [_FIX_REGISTRY[entry["fix_command"]] for entry in actionable]
    errors = _run_fix_steps(steps, conn, db)

    conn.close()
    clear_findings_cache()

    print()
    if errors:
        status.error(f"Done with {errors} error(s).", hint="Run 'siftd doctor' to verify.")
        return 1
    status.confirm("All fixes applied.", hint="Run 'siftd doctor' to verify.")
    return 0


def _doctor_fix_flagged(args, fix_command: str) -> int:
    """Run a specific fix directly by fix_command key (flag-discriminated form)."""
    db = resolve_db(args)
    if not db.exists():
        status.error(f"Database not found: {db}")
        return 1

    conn = open_database(db)
    errors = _run_fix_steps([_FIX_REGISTRY[fix_command]], conn, db)
    conn.close()
    return 1 if errors else 0


# Fix registry: maps fix_command → (label, callable(conn, db_path) → str)
def _fix_ingest(conn, db_path):
    from siftd.api import run_ingest

    # Surface the remote first-egress disclosure live, BEFORE content leaves the machine —
    # auto-index only emits it through on_notice (else it lands on a discarded result).
    # Matches the ingest command's on_notice → status.info in its plain-text mode.
    stats = run_ingest(db_path=db_path, on_notice=status.info).stats
    if stats is None:
        return "0 file(s) ingested, 0 skipped"
    return f"{stats.files_ingested} file(s) ingested, {stats.files_skipped} skipped"


def _fix_rebuild_fts(conn, db_path):
    from siftd.api.search import rebuild_fts_index

    rebuild_fts_index(conn)
    return "FTS index rebuilt"


def _fix_embed(conn, db_path):
    from siftd.api.search import build_index

    result = build_index(db_path=db_path, rebuild=False)
    return f"{result.get('chunks_added', 0)} chunk(s) indexed"


def _fix_embed_rebuild(conn, db_path):
    from siftd.api.search import build_index

    result = build_index(db_path=db_path, rebuild=True)
    return f"{result.get('chunks_added', 0)} chunk(s) indexed"


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
            (SELECT COUNT(*) FROM event_tool_call WHERE result_hash = content_blobs.hash), 0
        )
        WHERE ref_count != COALESCE(
            (SELECT COUNT(*) FROM event_tool_call WHERE result_hash = content_blobs.hash), 0
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
    "siftd embed": ("Indexing embeddings", _fix_embed),
    "siftd embed --rebuild": ("Rebuilding embeddings index", _fix_embed_rebuild),
    "siftd backfill --git-remote": ("Backfilling git remote URLs", _fix_backfill_git_remote),
    "siftd doctor fix --pending-tags": ("Cleaning up stale sessions", _fix_pending_tags),
    "siftd doctor fix --blob-refcount": ("Repairing content blob ref counts", _fix_blob_refcount),
    "siftd doctor fix --triggers": ("Recreating blob ref-count triggers", _fix_blob_triggers),
}


def _filter_findings(findings, *, json_mode: bool, drop_hints: bool = False):
    exclude = "text" if json_mode else "json"
    out = [f for f in findings if f.channel != exclude]
    if drop_hints:
        out = [f for f in out if f.severity != "hint"]
    return out


def _doctor_run(args, check_names: list[str] | None = None, show_fixes: bool = False) -> int:
    """Run doctor checks and display findings."""
    db = Path(args.db) if args.db else None
    deep = getattr(args, "deep", False)
    fast = getattr(args, "fast", False)

    # JSON output — no progress, no painted rendering
    if args.json:
        return _doctor_run_json(args, check_names, show_fixes, db, deep, fast)

    # TTY with Unicode support — painted progress + themed findings
    from siftd.output.common import supports_unicode

    if sys.stdout.isatty() and supports_unicode():
        return _doctor_run_painted(args, check_names, show_fixes, db, deep, fast)

    # Piped, or a terminal that can't render the Unicode glyphs — plain ASCII.
    return _doctor_run_plain(args, check_names, show_fixes, db, deep, fast)


def _doctor_run_json(args, check_names, show_fixes, db, deep=False, fast=False) -> int:
    from siftd.api import run_checks

    try:
        findings = run_checks(checks=check_names or None, db_path=db, deep=deep, fast=fast)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    findings = _filter_findings(findings, json_mode=True, drop_hints=getattr(args, "no_hints", False))

    findings.sort(key=lambda f: (status.severity_rank(f.severity), f.check))

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
                "target": f.target,
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


def _doctor_run_plain(args, check_names, show_fixes, db, deep=False, fast=False) -> int:
    from siftd.api import run_checks

    try:
        findings = run_checks(checks=check_names or None, db_path=db, deep=deep, fast=fast)
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    findings = _filter_findings(findings, json_mode=False, drop_hints=getattr(args, "no_hints", False))

    if not findings:
        status.info("No issues found.")
        return 0

    findings.sort(key=lambda f: (status.severity_rank(f.severity), f.check))

    from siftd.output.status import severity_glyph

    for finding in findings:
        glyph, _ = severity_glyph(finding.severity, as_ascii=True)
        print(f"[{glyph}] {finding.check}: {finding.message}")
        if finding.fix_command and not show_fixes:
            print(f"    Fix: {finding.fix_command}")

    error_count = sum(1 for f in findings if f.severity == "error")
    warning_count = sum(1 for f in findings if f.severity == "warning")
    info_count = sum(1 for f in findings if f.severity == "info")
    print()
    print(
        f"Found {fmt_count(len(findings))} issue(s): {fmt_count(error_count)} error, "
        f"{fmt_count(warning_count)} warning, {fmt_count(info_count)} info"
    )

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


def _doctor_run_painted(args, check_names, show_fixes, db, deep=False, fast=False) -> int:
    from siftd.api import list_checks, run_checks
    from siftd.doctor.view import render_findings_block, render_progress_block
    from siftd.output.live import LiveRegion

    all_checks = list_checks()
    if check_names:
        all_checks = [c for c in all_checks if c.name in check_names]
    if fast:
        all_checks = [c for c in all_checks if c.cost == "fast"]
    elif not deep:
        all_checks = [c for c in all_checks if getattr(c, "cost", "") != "deep"]
    check_names_ordered = [c.name for c in all_checks]

    drop_hints = getattr(args, "no_hints", False)
    completed: dict[str, list] = {}

    # LiveRegion brings the shared gate + throttle + log-quiesce (checks log
    # warnings mid-run; quiesce keeps them off the relative-addressed frame). The
    # panel repaints on each check completion (on_check_done fires on this thread
    # from run_checks' as_completed loop — no extra threading): the bar fills, the
    # just-resolved check rides the label, issues accumulate. The settled report
    # is deposited as the final frame. force=True per event: checks finish in
    # bursts, so each completion is a moment, not a stream to throttle.
    live = LiveRegion()
    with live:

        def on_done(name, check_findings):
            completed[name] = _filter_findings(check_findings, json_mode=False, drop_hints=drop_hints)
            if live.active:
                live.update(render_progress_block(check_names_ordered, completed, current=name), force=True)

        if live.active:
            # Show the empty bar from t=0 (before the first check resolves).
            live.update(render_progress_block(check_names_ordered, completed), force=True)

        try:
            findings = run_checks(
                checks=check_names or None,
                db_path=db,
                deep=deep,
                fast=fast,
                on_check_done=on_done,
            )
        except FileNotFoundError as e:
            live.finalize()
            print(str(e))
            return 1
        except ValueError as e:
            live.finalize()
            print(f"Error: {e}")
            return 1

        findings = _filter_findings(findings, json_mode=False, drop_hints=drop_hints)
        if live.active:
            live.finalize(
                render_findings_block(
                    findings, show_fixes=show_fixes, total_checks=len(check_names_ordered)
                )
            )

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

    # Warn about fix-only flags being used outside the 'fix' subcommand.
    if action != "fix":
        for flag, name in (
            ("pending_tags", "--pending-tags"),
            ("blob_refcount", "--blob-refcount"),
            ("triggers", "--triggers"),
        ):
            if getattr(args, flag, False):
                status.info(f"{name} ignored without 'fix' subcommand")

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
        default=None,
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
  siftd backfill --filter-binary --dry-run  # preview what would be filtered
  siftd backfill --git-remote       # backfill git remote URLs for workspaces missing them
  siftd backfill --models           # re-parse model names to canonical form (reprices on next open)
  siftd backfill --pricing          # reproject the pricing reference + rebuild cost (after editing prices)""",
    )
    p_backfill.add_argument("--models", action="store_true", help="Re-parse raw model names to canonical form (e.g. claude-haiku-4.5 -> claude-haiku-4-5)")
    p_backfill.add_argument("--pricing", action="store_true", help="Reproject the pricing reference and rebuild the cost rollup (run after editing pricing.toml)")
    p_backfill.add_argument("--shell-tags", action="store_true", help="Tag shell.execute calls with shell:* categories")
    p_backfill.add_argument("--derivative-tags", action="store_true", help="Tag conversations containing siftd search/query as siftd:derivative")
    p_backfill.add_argument("--filter-binary", action="store_true", help="Filter binary content (images, base64) from existing blobs")
    p_backfill.add_argument("--git-remote", action="store_true", help="Backfill git remote URLs for workspaces missing them (use 'siftd migrate --merge-workspaces' to also collapse duplicates)")
    p_backfill.add_argument("--dry-run", action="store_true", help="Preview changes without applying (use with --filter-binary or --git-remote)")
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
    p_doctor.add_argument("--fast", action="store_true", help="Run only fast checks (skips slow and deep).")
    p_doctor.add_argument("--blob-refcount", action="store_true", dest="blob_refcount", help="Re-derive blob ref counts and sweep orphans (use with 'fix').")
    p_doctor.add_argument("--triggers", action="store_true", help="Recreate blob ref-count triggers (use with 'fix').")
    p_doctor.add_argument("--no-hints", action="store_true", dest="no_hints", help="Suppress hint-severity findings.")
    p_doctor.set_defaults(func=cmd_doctor)
