from siftd.api import export as api_export


def test_export_document_builds_default_fidelity_when_none(monkeypatch):
    monkeypatch.setattr("siftd.api.export.export_conversations", lambda **kwargs: [])

    artifact = api_export.export_document(format="json", fidelity=None)

    assert artifact.media_type == "application/json"
    assert artifact.filename == "siftd-export-0.json"
    assert artifact.count == 0
    assert artifact.content == "[]"
