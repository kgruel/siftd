from siftd.serve.auth import create_auth_middleware


def test_focus_smoke_for_serve_auth_lane():
    assert create_auth_middleware({}).__name__ == "SiftdAuthMiddleware"
