"""Detects declared tool aliases the database doesn't follow.

An adapter's ``TOOL_ALIASES`` maps its raw tool names to canonical ones
(``bash`` -> ``shell.execute``). A DB that ingested the harness before the
declaration stored a self-alias instead (``bash`` -> ``bash``), and that alias
outlived the declaration, so ``--tool shell.execute`` missed every such call.
Ingest now repairs it per harness; a DB that only receives merges (a server)
needs the fix run against it.
"""

from siftd.doctor.checks import CheckContext, CheckCost, Finding

FIX = "siftd doctor fix --tool-aliases"


class ToolAliasesCheck:
    name = "tool-aliases"
    description = "Stored tool aliases that don't follow an adapter's declared canonical name"
    has_fix = True
    requires_db = True
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.adapters.registry import declared_tool_aliases
        from siftd.storage.sqlite import stale_tool_aliases

        stale = stale_tool_aliases(ctx.get_db_conn(), declared_tool_aliases(ctx.get_adapters()))
        return [
            Finding(
                check=self.name,
                severity="warning",
                message=(
                    f"{s['harness']}: '{s['raw_name']}' is stored as '{s['stored']}', not '{s['declared']}'"
                    f" ({s['calls']} call(s) missing from --tool {s['declared']})"
                ),
                fix_available=True,
                fix_command=FIX,
                context=s,
            )
            for s in stale
        ]
