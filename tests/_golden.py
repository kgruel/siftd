"""Canonical (collapsed) serialization for adapter golden fixtures.

Shared by ``conftest.assert_golden`` (compare) and ``scripts/gen-adapter-fixture.sh``
(generate) so the two never drift.

``collapse`` mirrors ``dataclasses.asdict`` but OMITS any field whose value
equals its declared default (``default`` or ``default_factory()``). The golden
fixtures therefore encode only what a case actually exercises: adding a new
*defaulted* field to a domain model (e.g. ``Conversation.attributes``) never
ripples across every ``expected.json``. The contract stays strict on
*non-default* values — a wrong/missing/extra non-default field still fails —
it only stops ritualizing fields that equal their default everywhere.

``GOLDEN_TZ`` is the other half of that shared contract: an adapter that
resolves a naive log timestamp against the host zone (``aider``) would
otherwise bake the generating machine's offset into ``expected.json``, and the
comparing machine would read it as a diff. Both sides pin this zone.

Dependency-light on purpose (stdlib ``dataclasses`` only) so the standalone
generator heredoc can ``import _golden`` without dragging in pytest/siftd.
"""

import dataclasses

# The zone every golden fixture is generated and compared under. Not UTC
# because UTC is the "right" zone to store in — because it is *a* zone, and
# the two sides have to name the same one.
GOLDEN_TZ = "UTC"


def collapse(obj):
    """Recursively serialize ``obj`` to JSON-able dict/list/scalars, dropping
    dataclass fields whose value equals their declared default."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out = {}
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if f.default is not dataclasses.MISSING:
                default = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                default = f.default_factory()
            else:
                default = dataclasses.MISSING  # required field — always emit
            if default is not dataclasses.MISSING and value == default:
                continue
            out[f.name] = collapse(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [collapse(v) for v in obj]
    if isinstance(obj, dict):
        return {k: collapse(v) for k, v in obj.items()}
    return obj
