"""Phase 9: the backend serves the built frontend (index + hashed assets) without weakening the API contract."""

import asyncio
import logging

import pytest
from starlette.testclient import TestClient

from storyflow.api.app import ASSET_CACHE_CONTROL, FRONTEND_BUILD_HINT, create_app
from test_api import make_runtime

INDEX = "<!doctype html><title>StoryFlow</title><div id=root></div>"
JS = "console.log('app');"
SECRET = "TOP-SECRET-OUTSIDE-DIST"


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "site" / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text(JS, encoding="utf-8")
    (root / "assets" / "index-abc123.css").write_text("body{}", encoding="utf-8")
    (root / "assets" / "sub").mkdir()
    (root / "assets" / "sub" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "site" / "secret.txt").write_text(SECRET, encoding="utf-8")   # sibling of dist
    (root / "assets" / "unicode-ü.js").write_text("u", encoding="utf-8")
    return root


@pytest.fixture
def rt(tmp_path):
    app = make_runtime(tmp_path, sleep=lambda s: None)
    yield app
    app.close()


@pytest.fixture
def client(rt, dist):
    return TestClient(create_app(rt, frontend_dir=dist))


def assert_json_404(resp, tmp_path=None):
    assert resp.status_code == 404, (resp.status_code, resp.text[:200])
    assert resp.json() == {"error": {"code": "not_found", "message": "not found", "details": {}}}
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert SECRET not in resp.text
    if tmp_path is not None:
        assert str(tmp_path) not in resp.text and tmp_path.as_posix() not in resp.text


def test_index_at_root_with_no_cache_and_nosniff(client):
    r = client.get("/")
    assert r.status_code == 200 and r.text == INDEX
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_assets_are_immutable_and_nosniff(client):
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200 and r.text == JS
    assert r.headers["cache-control"] == ASSET_CACHE_CONTROL == "public, max-age=31536000, immutable"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "javascript" in r.headers["content-type"]
    css = client.get("/assets/index-abc123.css")
    assert css.headers["content-type"].startswith("text/css") and css.headers["cache-control"] == ASSET_CACHE_CONTROL
    assert client.get("/assets/sub/x.txt").status_code == 200
    assert client.get("/assets/unicode-%C3%BC.js").status_code == 200


def test_conditional_get_keeps_cache_header(client):
    first = client.get("/assets/index-abc123.js")
    again = client.get("/assets/index-abc123.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.headers["x-content-type-options"] == "nosniff"


def test_head_supported(client):
    for path, cache in (("/", "no-cache"), ("/assets/index-abc123.js", ASSET_CACHE_CONTROL)):
        r = client.head(path)
        assert r.status_code == 200 and r.content == b""
        assert r.headers["cache-control"] == cache and r.headers["x-content-type-options"] == "nosniff"
        assert int(r.headers["content-length"]) > 0


def test_other_methods_on_static_use_error_contract(client):
    r = client.post("/")
    assert r.status_code == 405 and r.json()["error"]["code"] == "validation"
    r = client.post("/assets/index-abc123.js")
    assert r.status_code == 405 and r.json()["error"]["code"] == "validation"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_unknown_api_paths_stay_json_not_html(client):
    for path in ("/api/nope", "/api/", "/api/workflows/does-not-exist/nope", "/api/assets/index-abc123.js"):
        assert_json_404(client.get(path))
    assert client.head("/api/nope").status_code == 404


def test_unknown_non_api_paths_are_json_404_without_spa_fallback(client):
    for path in ("/nope", "/workflows/abc", "/index.html", "/assets/missing.js", "/assets/", "/assets",
                 "/favicon.ico"):
        assert_json_404(client.get(path))


TRAVERSAL = [
    "/assets/../index.html", "/assets/%2e%2e/index.html", "/assets/%2E%2E/%2E%2E/secret.txt",
    "/assets/..%2fsecret.txt", "/assets/..%5csecret.txt", "/assets/..\\secret.txt",
    "/assets/../../secret.txt", "/assets/sub/../../../secret.txt", "/assets//secret.txt",
    "/assets/%2fsecret.txt", "/assets/C:/Windows/win.ini", "/assets/C:\\Windows\\win.ini",
    "/assets/..%252fsecret.txt", "/assets/....//secret.txt", "/assets/index-abc123.js%00.png",
    "/assets/%00", "/assets/index-abc123.js/", "/assets/sub/..\\..\\..\\secret.txt",
]


@pytest.mark.parametrize("path", TRAVERSAL)
def test_traversal_battery_via_client(client, tmp_path, path):
    r = client.get(path)
    if r.status_code == 200:  # the client may normalise a dot-segment path to a legitimate in-dist file
        assert r.text in (INDEX, JS) and SECRET not in r.text
        assert r.headers["x-content-type-options"] == "nosniff"
    else:
        assert_json_404(r, tmp_path)


RAW_PATHS = ["/assets/../index.html", "/assets/../../secret.txt", "/assets/..\\secret.txt", "/assets/..\\..\\secret.txt",
             "/assets/../../../../../../etc/passwd", "/assets/../../../Windows/win.ini", "/assets/C:/Windows/win.ini",
             "/assets//../secret.txt", "/assets/sub/../../../secret.txt", "/assets/x\x00.js"]


def _raw_get(app, raw_path, method="GET"):
    """Drive the ASGI app with an UNNORMALISED decoded path (what a raw socket client can send)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": raw_path, "raw_path": raw_path.encode("latin-1", "replace"), "root_path": "", "query_string": b"",
             "headers": [(b"host", b"127.0.0.1:8765")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8765)}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


@pytest.mark.parametrize("path", RAW_PATHS)
def test_raw_traversal_never_leaves_dist(rt, dist, tmp_path, path):
    app = create_app(rt, frontend_dir=dist)
    status, headers, body = _raw_get(app, path)
    text = body.decode("utf-8", "replace")
    assert SECRET not in text and "root:" not in text and "[fonts]" not in text.lower()
    assert str(tmp_path) not in text and tmp_path.as_posix() not in text
    assert headers[b"x-content-type-options"] == b"nosniff"
    if path != "/assets/../index.html":  # (an in-dist file is the only acceptable 200)
        assert status == 404, (status, text[:200])
        assert b'"code":"not_found"' in body.replace(b" ", b"")


def test_missing_dist_json_404_warning_and_api_works(rt, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="storyflow.api"):
        api = create_app(rt, frontend_dir=tmp_path / "no-such-dist")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "frontend" in r.getMessage()]
    assert len(warnings) == 1 and FRONTEND_BUILD_HINT in warnings[0].getMessage()
    assert "no-such-dist" not in warnings[0].getMessage()
    client = TestClient(api)
    assert_json_404(client.get("/"), tmp_path)
    assert_json_404(client.get("/assets/index-abc123.js"))
    assert client.get("/api/health").status_code == 200
    assert api.state.frontend_served is False


def test_dir_without_index_counts_as_missing(rt, tmp_path, caplog):
    (tmp_path / "empty").mkdir()
    with caplog.at_level(logging.WARNING, logger="storyflow.api"):
        api = create_app(rt, frontend_dir=tmp_path / "empty")
    assert api.state.frontend_served is False and any(FRONTEND_BUILD_HINT in m for m in caplog.messages)


def test_no_frontend_dir_means_no_serving_and_no_warning(rt, caplog):
    with caplog.at_level(logging.WARNING, logger="storyflow.api"):
        api = create_app(rt)
    assert not [m for m in caplog.messages if "frontend" in m]
    assert_json_404(TestClient(api).get("/"))


def test_api_endpoints_unaffected_and_docs_disabled(client):
    assert client.get("/api/health").status_code == 200
    r = client.post("/api/workflows", json={"name": "chan", "config": {
        "source": {"video_id": "vid", "languages": ["en"]}, "story": {"branch": "b"}, "tts": {"voice": "v"}}})
    assert r.status_code == 201
    assert client.get("/api/workflows").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert_json_404(client.get(path))


def test_host_guard_applies_to_static(client):
    for path in ("/", "/assets/index-abc123.js"):
        r = client.get(path, headers={"Host": "evil.example.com"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "validation"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert INDEX not in r.text and JS not in r.text


def test_cors_unchanged_on_static(client):
    ok = client.get("/", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = client.get("/", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in bad.headers
    bad_asset = client.get("/assets/index-abc123.js", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in bad_asset.headers
    pre = client.options("/", headers={"Origin": "http://evil.example.com", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in pre.headers


def test_real_built_dist_is_servable_when_present(rt):
    from pathlib import Path
    real = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (real / "index.html").is_file():
        pytest.xfail("frontend/dist not built (cd frontend && npm ci && npm run build)")
    client = TestClient(create_app(rt, frontend_dir=real))
    index = client.get("/")
    assert index.status_code == 200 and "/assets/" in index.text
    for asset in (real / "assets").iterdir():
        r = client.get(f"/assets/{asset.name}")
        assert r.status_code == 200 and r.headers["cache-control"] == ASSET_CACHE_CONTROL
