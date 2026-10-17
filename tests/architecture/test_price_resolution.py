"""Which price a response gets is decided in one place; the sites that read `pricing` are enumerated.

siftd's cost is the model maker's list price for the tokens: a response is priced at
the provider that served it when a price is filed there, else at the model's maker
(`models.creator`), else not at all. That is a join on `pricing` through a computed
provider id, and it used to be spelled three ways — the rollup's `COALESCE(provider_id,
harness-source provider)`, the unpriced report's `NOT EXISTS` (which counted a NULL
provider as priced whenever any price existed), and `cost.sql`'s plain
`pr.provider_id = er.provider_id` — each with a different idea of what a "multi" response is
priced at, so the cost and the "unpriced" report could disagree about the same response.

So the rule is written once (`storage/sql_helpers.py`: `price_provider_id_sql`,
`price_join_sql`) and this is the CLAUDE.md "enumerable property with a shrink-only
allowlist":

1. **A SQL literal that selects `FROM pricing` or joins it is a place that can start a
   resolution of its own.** The home is excluded; each survivor says why it is not one.
2. **A shipped report does not read `pricing` at all.** `builtin_queries/*.sql` cannot
   call the helper, so a report that needs a cost reads `usage_by_conv_model.cost`.

Scope and limits. Both match source text: a join on `pricing` spelled through a
variable table name is invisible, and a Python-side lookup of a price that never
writes the table name is invisible to (1). Neither can say whether an allowlisted
site *stays* innocent, only that its reason is written where a reviewer reads it.

The allowlist is shrink-only.
"""

import re

from architecture.support import SRC, literal_matching, sites_by_function

PRICING_READ = re.compile(r"\b(?:FROM|JOIN)\s+pricing\b", re.IGNORECASE)

HOME = "storage/sql_helpers.py"

# (module, function) -> why a read of `pricing` there is not a second price resolution. Shrink-only.
PRICING_READS: dict[tuple[str, str], str] = {
    ("storage/usage_rollup.py", "rebuild_usage_by_conv_model"): (
        "The v9-era branch, FROZEN so the v9 migration's cost-parity assertion compares like with "
        "like; a database reaches it only mid-migration, before the pricing cache columns exist."
    ),
    ("storage/sqlite.py", "_migrate_v11_pricing_reference"): (
        "Asserts the reprojection corrected each reference row; it reads rows by (model, provider) name."
    ),
    ("storage/sqlite.py", "get_priced_models_without_provenance"): (
        "Lists price rows with no provenance for models in use; it resolves no response's price."
    ),
    ("api/slice.py", "_populate_slice"): (
        "Copies the price rows of the copied models and providers; the providers it copies are the "
        "rule's inputs (serving, harness source, maker)."
    ),
}


def test_pricing_reads_are_all_accounted_for():
    sites = sites_by_function(literal_matching(PRICING_READ, "pricing-read"), exclude=HOME)
    offenders = sorted(set(sites) - set(PRICING_READS))
    assert not offenders, (
        f"SQL reads `pricing` in {offenders}. Which price a response gets is decided once, by "
        "`siftd.storage.sql_helpers.price_provider_id_sql`: join through `price_join_sql`, or read "
        "`usage_by_conv_model.cost`. If this site lists or copies price rows and truly resolves no "
        "response's price, write why in PRICING_READS."
    )


def test_every_listed_pricing_read_still_exists():
    """The ratchet's own guard: an entry that no longer matches is a claim nothing tests."""
    sites = sites_by_function(literal_matching(PRICING_READ, "pricing-read"), exclude=HOME)
    stale = sorted(set(PRICING_READS) - set(sites))
    assert not stale, f"drop {stale} from PRICING_READS: it no longer reads `pricing`"


def test_the_home_still_holds_the_rule():
    """The exclusion above is only honest while the home is where the join lives."""
    sites = sites_by_function(literal_matching(PRICING_READ, "pricing-read"))
    assert {fn for (module, fn) in sites if module == HOME} == {"price_provider_id_sql", "price_join_sql"}


def test_shipped_reports_read_cost_from_the_rollup_not_from_pricing():
    holders = sorted(
        path.name
        for path in (SRC / "builtin_queries").glob("*.sql")
        if PRICING_READ.search(path.read_text())
    )
    assert not holders, (
        f"{holders} read `pricing` directly. A report cannot call the price rule, so it must read "
        "`usage_by_conv_model.cost`, where the rule is already applied."
    )


def test_allowlist_only_shrinks():
    assert set(PRICING_READS) == {
        ("storage/usage_rollup.py", "rebuild_usage_by_conv_model"),
        ("storage/sqlite.py", "_migrate_v11_pricing_reference"),
        ("storage/sqlite.py", "get_priced_models_without_provenance"),
        ("api/slice.py", "_populate_slice"),
    }, "PRICING_READS is shrink-only: a new site needs its reason written here, where a reviewer reads it."
