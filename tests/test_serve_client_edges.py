import json

import pytest

from siftd.serve import client


class _Resp:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _Conn:
    def __init__(self, resp):
        self.resp = resp
        self.closed = False
        self.req = None

    def request(self, method, path, body=None, headers=None):
        self.req = (method, path, body, headers)

    def getresponse(self):
        return self.resp

    def close(self):
        self.closed = True


def test_parse_target_rejects_unsupported_scheme():
    with pytest.raises(ValueError, match="Unsupported serve URL scheme"):
        client._parse_target("ftp://example.com")


def test_default_base_url_env(monkeypatch):
    monkeypatch.setenv("SIFTD_SERVE_URL", "http://x:1")
    assert client.default_base_url() == "http://x:1"


def test_conn_selects_https(monkeypatch):
    monkeypatch.setattr("siftd.serve.client.HTTPSConnection", lambda *a, **k: "https")
    t = client.ServeTarget("https", "h", 443, "")
    assert client._conn(t, 0.1) == "https"


def test_get_json_handles_non_200_and_invalid_json(monkeypatch):
    c1 = _Conn(_Resp(status=503, body=b"{}"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: c1)
    with pytest.raises(client.ServeUnavailable, match="HTTP 503"):
        client._get_json("http://127.0.0.1:8484", "/api/v1/health")
    assert c1.closed

    c2 = _Conn(_Resp(status=200, body=b"not-json"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: c2)
    with pytest.raises(client.ServeUnavailable, match="Invalid JSON"):
        client._get_json("http://127.0.0.1:8484", "/api/v1/health")


def test_post_json_happy_path_and_shape_validation(monkeypatch):
    ok = _Conn(_Resp(status=201, body=json.dumps({"ok": True}).encode()))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: ok)
    out = client._post_json("http://127.0.0.1:8484", "/api/v1/x", body={"a": 1})
    assert out == {"ok": True}
    assert ok.req[0] == "POST" and ok.req[1] == "/api/v1/x"

    bad_shape = _Conn(_Resp(status=200, body=b"[]"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: bad_shape)
    with pytest.raises(client.ServeUnavailable, match="expected object"):
        client._post_json("http://127.0.0.1:8484", "/api/v1/x", body={})


def test_probe_health_rejects_unrecognized_payload(monkeypatch):
    monkeypatch.setattr("siftd.serve.client._get_json", lambda *a, **k: {"status": "bad"})
    with pytest.raises(client.ServeUnavailable, match="unrecognized health payload"):
        client.probe_health(base_url="http://127.0.0.1:8484")


def test_get_json_success_query_and_shape_error(monkeypatch):
    ok = _Conn(_Resp(status=200, body=b'{"ok":true}'))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: ok)
    out = client._get_json("http://127.0.0.1:8484/base", "/api/v1/search", params={"tag": ["a", "b"]})
    assert out == {"ok": True}
    assert ok.req[1].startswith("/base/api/v1/search?") and "tag=a" in ok.req[1] and "tag=b" in ok.req[1]

    bad_shape = _Conn(_Resp(status=200, body=b"[]"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: bad_shape)
    with pytest.raises(client.ServeUnavailable, match="expected object"):
        client._get_json("http://127.0.0.1:8484", "/api/v1/search")


def test_post_json_non_200_and_invalid_json(monkeypatch):
    c1 = _Conn(_Resp(status=500, body=b"{}"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: c1)
    with pytest.raises(client.ServeUnavailable, match="HTTP 500"):
        client._post_json("http://127.0.0.1:8484", "/api/v1/x", body={})

    c2 = _Conn(_Resp(status=200, body=b"not-json"))
    monkeypatch.setattr("siftd.serve.client._conn", lambda *_a, **_k: c2)
    with pytest.raises(client.ServeUnavailable, match="Invalid JSON"):
        client._post_json("http://127.0.0.1:8484", "/api/v1/x", body={})


def test_probe_health_success_and_wrappers(monkeypatch):
    calls = []

    def fake_get(base_url, path, **kwargs):
        calls.append((base_url, path, kwargs))
        if path == "/api/v1/health":
            return {"status": "ok", "service": "siftd"}
        return {"items": []}

    monkeypatch.setattr("siftd.serve.client._get_json", fake_get)
    assert client.probe_health(base_url="http://127.0.0.1:8484")["service"] == "siftd"
    client.search(base_url="http://127.0.0.1:8484", params={"q": "x"})
    client.stats(base_url="http://127.0.0.1:8484")
    assert [c[1] for c in calls] == ["/api/v1/health", "/api/v1/search", "/api/v1/stats"]
