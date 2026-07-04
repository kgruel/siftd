"""I5 — caveat round-trip across the delegation wire.

Caveats are the editorial-honesty channel (stale index, degraded mode,
truncation). For a thin client this is knowable only server-side, so the
serve envelope's top-level ``caveats`` key must survive the trip back into
typed ``Finding`` objects. ``serialize_caveats`` (serve side) and
``deserialize_caveats`` (client side) are inverses; these pin that contract
and the defensive behavior that keeps a newer server from breaking an older
client.
"""

from __future__ import annotations

from siftd.api.deserialize import deserialize_caveats
from siftd.doctor.checks import Finding
from siftd.serialization.serve_fmt import serialize_caveats


def _envelope(findings):
    """Mimic the serve envelope: result rows plus the caveats key."""
    return {"results": [], "caveats": serialize_caveats(findings)}


def test_roundtrip_preserves_all_fields():
    findings = [
        Finding(check="embeddings-stale", severity="warning",
                message="3 conversations not indexed", fix_available=True,
                fix_command="siftd embed", context={"count": 3}),
        Finding(check="fresh-corpus", severity="info",
                message="Corpus contains 2 conversations", fix_available=False,
                context={"total": 2}, target=None),
    ]
    restored = deserialize_caveats(_envelope(findings))
    assert restored == findings  # dataclass __eq__ compares every field


def test_missing_caveats_key_yields_empty():
    assert deserialize_caveats({"results": []}) == []


def test_non_dict_body_yields_empty():
    assert deserialize_caveats(None) == []
    assert deserialize_caveats([1, 2, 3]) == []


def test_unknown_keys_are_dropped_not_fatal():
    """A server on a newer Finding shape must degrade, not raise."""
    body = {"caveats": [{
        "check": "future", "severity": "info", "message": "m",
        "fix_available": False, "brand_new_field": "ignored",
    }]}
    restored = deserialize_caveats(body)
    assert len(restored) == 1
    assert restored[0].check == "future"
    assert not hasattr(restored[0], "brand_new_field")


def test_malformed_entries_are_skipped():
    body = {"caveats": [
        "not-a-dict",
        {"severity": "info"},  # missing required check/message/fix_available
        {"check": "ok", "severity": "info", "message": "m", "fix_available": False},
    ]}
    restored = deserialize_caveats(body)
    assert len(restored) == 1
    assert restored[0].check == "ok"


def test_empty_caveats_list_yields_empty():
    assert deserialize_caveats({"caveats": []}) == []
