import os
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from storyflow.api.artifacts import router
from storyflow.api.errors import register_error_handlers
from storyflow.artifacts import ArtifactStore

WAV = b"RIFF" + bytes(range(256)) * 4  # 1028 bytes
NOT_FOUND = {"error": {"code": "not_found", "message": "not found", "details": {}}}


def make_app(store):
    app = FastAPI()
    app.state.container = types.SimpleNamespace(store=store)
    register_error_handlers(app)
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "store"
    store = ArtifactStore(root)
    store.write("projects/p1/story/g1/story.md", "# Truyen\nnoi dung".encode())
    store.write("projects/p1/source/0001/source.txt", b"source text")
    store.write("projects/p1/canon/c1/canon.json", b'{"a": 1}')
    store.write("projects/p1/tts/t1/chunks/0001.txt", b"chunk one")
    store.write("projects/p1/audio/t1/run-001/0001.wav", WAV)
    store.write("projects/p1/audio/t1/run-001/x.exe", b"MZ")
    store.write("top.txt", b"root level")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-OUTSIDE")
    (root / "projects" / "p1" / ".tmp-abc.txt").write_bytes(b"temp")
    client = TestClient(make_app(store))
    return types.SimpleNamespace(store=store, root=root, client=client, tmp=tmp_path, app=client.app)


def test_happy_paths(env):
    cases = {
        "projects/p1/story/g1/story.md": (b"# Truyen\nnoi dung", "text/markdown; charset=utf-8"),
        "projects/p1/source/0001/source.txt": (b"source text", "text/plain; charset=utf-8"),
        "projects/p1/canon/c1/canon.json": (b'{"a": 1}', "application/json"),
        "projects/p1/tts/t1/chunks/0001.txt": (b"chunk one", "text/plain; charset=utf-8"),
        "projects/p1/audio/t1/run-001/0001.wav": (WAV, "audio/wav"),
    }
    for rel, (body, ctype) in cases.items():
        r = env.client.get("/api/artifacts/" + rel)
        assert r.status_code == 200, rel
        assert r.content == body
        assert r.headers["content-type"] == ctype
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-cache"
        cd = r.headers["content-disposition"]
        assert cd.startswith("inline") and rel.rsplit("/", 1)[-1] in cd and "/" not in cd
        assert r.headers["etag"] and r.headers["last-modified"]
        assert str(env.tmp) not in "".join(f"{k}{v}" for k, v in r.headers.items())


def test_head(env):
    r = env.client.head("/api/artifacts/projects/p1/audio/t1/run-001/0001.wav")
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-length"] == str(len(WAV))
    assert r.headers["content-type"] == "audio/wav"


def test_range_partial_and_unsatisfiable(env):
    url = "/api/artifacts/projects/p1/audio/t1/run-001/0001.wav"
    r = env.client.get(url, headers={"Range": "bytes=4-19"})
    assert r.status_code == 206
    assert r.content == WAV[4:20]
    assert r.headers["content-range"] == f"bytes 4-19/{len(WAV)}"
    r = env.client.get(url, headers={"Range": "bytes=-10"})
    assert r.status_code == 206 and r.content == WAV[-10:]
    r = env.client.get(url, headers={"Range": f"bytes={len(WAV) + 50}-"})
    assert r.status_code == 416
    assert "content-range" in r.headers


def test_malformed_range_is_ignored(env):
    url = "/api/artifacts/projects/p1/audio/t1/run-001/0001.wav"
    for bad in ("bytes=abc", "items=1-2", "garbage"):
        r = env.client.get(url, headers={"Range": bad})
        assert r.status_code in (200, 400, 416)
        if r.status_code == 200:
            assert r.content == WAV


def test_etag_if_none_match(env):
    url = "/api/artifacts/projects/p1/story/g1/story.md"
    etag = env.client.get(url).headers["etag"]
    r = env.client.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b""


ATTACKS = [
    "../secret.txt",
    "projects/../../secret.txt",
    "projects/p1/../../../secret.txt",
    "projects/p1/%2e%2e/%2e%2e/%2e%2e/secret.txt",
    "projects/p1/%2E%2E/%2E%2E/%2E%2E/secret.txt",
    "projects/p1/%252e%252e/%252e%252e/secret.txt",
    "projects/p1/..%2f..%2f..%2fsecret.txt",
    "projects/p1/..%5c..%5c..%5csecret.txt",
    "projects/p1/..\\..\\..\\secret.txt",
    "projects\\p1\\story\\g1\\story.md",
    "projects/p1/..;/..;/secret.txt",
    "projects/p1/%c0%ae%c0%ae/%c0%ae%c0%ae/secret.txt",
    "projects/p1/%e0%80%ae%e0%80%ae/secret.txt",
    "projects/p1/story/g1/story.md%00.png",
    "projects/p1/story/g1/story.md%00",
    "projects/p1/story/g1/%00story.md",
    "projects/p1/story/g1/story%01.md",
    "/etc/passwd",
    "%2fetc%2fpasswd",
    "//etc/passwd",
    "C:/Windows/win.ini",
    "C%3a/Windows/win.ini",
    "C:\\Windows\\win.ini",
    "%5c%5cserver%5cshare%5cx.txt",
    "\\\\server\\share\\x.txt",
    "\\\\?\\C:\\Windows\\win.ini",
    "projects/p1/CON",
    "projects/p1/CON.txt",
    "projects/p1/NUL.txt",
    "projects/p1/aux.md",
    "projects/p1/COM1.json",
    "projects/p1/story/g1/story.md.",
    "projects/p1/story/g1/story.md%20",
    "projects/p1/story/g1/story.md::$DATA",
    "projects/p1/story/g1/story.md:stream",
    "projects/p1/story/g1/story.md:stream.txt",
    "projects/p1/story/g1/story.md%3a%3a%24DATA",
    "projects//p1/story/g1/story.md",
    "projects/p1//story/g1/story.md",
    "projects/p1/story/g1/story.md/",
    "projects/p1/story/g1/*.md",
    "projects/p1/story/g1/st?ry.md",
    "projects/p1/story/g1/~1.md",
    "projects/p1/" + "a" * 600 + ".txt",
    "projects/" + "/".join(["d"] * 300) + "/x.txt",
    "top.txt",
    "../store/top.txt",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_traversal_battery(env, attack):
    r = env.client.get("/api/artifacts/" + attack)
    assert r.status_code in (404, 422), attack
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] in ("not_found", "validation")
    text = r.text
    assert "TOP-SECRET-OUTSIDE" not in text and "root level" not in text
    assert "Traceback" not in text
    assert str(env.tmp) not in text and str(env.root) not in text and "store" not in text.replace("not found", "")
    assert attack not in text and attack.replace("%2e", ".") not in text
    if r.status_code == 404 and not attack.startswith(("..", "%2f", "//")):
        assert body == NOT_FOUND or body["error"]["code"] == "not_found"


def test_refusals_identical_to_missing(env):
    missing = env.client.get("/api/artifacts/projects/p1/story/g1/nope.md")
    assert missing.status_code == 404 and missing.json() == NOT_FOUND
    for rel in ("projects/p1/%2e%2e/%2e%2e/secret.txt", "projects/p1/..%5c..%5csecret.txt",
                "projects/p1/story", "projects/p1/story/g1/story.md::$DATA",
                "top.txt", "projects/p1/.tmp-abc.txt", "projects/p1/audio/t1/run-001/x.exe",
                "projects/p1/%252e%252e/x.txt", "C:/Windows/win.ini"):
        r = env.client.get("/api/artifacts/" + rel)
        assert (r.status_code, r.json()) == (missing.status_code, missing.json()), rel


def test_directory_tmp_extension_and_outside_projects(env):
    for rel in ("projects/p1/story/g1", "projects/p1/story/g1/", "projects",
                "projects/p1/.tmp-abc.txt", "projects/p1/audio/t1/run-001/x.exe", "top.txt"):
        r = env.client.get("/api/artifacts/" + rel)
        assert r.status_code == 404, rel
        assert r.json() == NOT_FOUND


def test_symlink_escape(env):
    link = env.root / "projects" / "p1" / "link.txt"
    try:
        os.symlink(env.tmp / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    r = env.client.get("/api/artifacts/projects/p1/link.txt")
    assert r.status_code == 404 and r.json() == NOT_FOUND
    assert "TOP-SECRET" not in r.text


def test_symlinked_directory_escape(env):
    outside = env.tmp / "outdir"
    outside.mkdir()
    (outside / "leak.txt").write_text("LEAKED")
    link = env.root / "projects" / "p1" / "dirlink"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    r = env.client.get("/api/artifacts/projects/p1/dirlink/leak.txt")
    assert r.status_code == 404 and "LEAKED" not in r.text


def test_size_cap(env):
    env.app.state.artifact_max_bytes = 100
    big = env.client.get("/api/artifacts/projects/p1/audio/t1/run-001/0001.wav")
    assert big.status_code == 422
    assert big.json() == {"error": {"code": "validation", "message": "artifact too large", "details": {}}}
    small = env.client.get("/api/artifacts/projects/p1/source/0001/source.txt")
    assert small.status_code == 200
    env.app.state.artifact_max_bytes = len(WAV)
    assert env.client.get("/api/artifacts/projects/p1/audio/t1/run-001/0001.wav").status_code == 200


def test_bodies_never_contain_absolute_paths(env):
    for rel in ("projects/p1/nope.md", "projects/p1/story", "../x.txt", "projects/p1/a%00.txt"):
        r = env.client.get("/api/artifacts/" + rel)
        assert str(env.tmp) not in r.text and str(env.root) not in r.text
        assert str(env.root).replace("\\", "/") not in r.text
