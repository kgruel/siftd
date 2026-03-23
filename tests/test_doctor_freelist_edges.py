from types import SimpleNamespace

from siftd.doctor.checks.freelist import FreelistCheck


def test_freelist_check_formats_large_waste_in_mb(monkeypatch):
    monkeypatch.setattr(
        "siftd.storage.sqlite.get_freelist_info",
        lambda _conn: {"freelist_count": 2048, "page_size": 1024, "page_count": 4096},
    )
    ctx = SimpleNamespace(get_db_conn=lambda: object(), db_path="/tmp/siftd.db")

    finding = FreelistCheck().run(ctx)[0]

    assert "2.0MB" in finding.message
    assert finding.context["wasted_bytes"] == 2097152
