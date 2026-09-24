"""Phase 6 hardening: request size / nesting limits and health-shaped regressions."""

import json

from fastapi.testclient import TestClient

from storyflow.api.app import MAX_BODY_BYTES, create_app
from test_api import make_runtime


def _client(tmp_path):
    rt = make_runtime(tmp_path, sleep=lambda s: None)
    return rt, TestClient(create_app(rt))


def _contract(resp, status, code):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_declared_oversized_body_is_413_validation(tmp_path):
    rt, client = _client(tmp_path)
    try:
        payload = json.dumps({"name": "x" * (MAX_BODY_BYTES + 10)})
        _contract(client.post("/api/workflows", content=payload,
                              headers={"Content-Type": "application/json"}), 413, "validation")
    finally:
        rt.close()


def test_streamed_oversized_body_without_content_length_is_413(tmp_path):
    rt, client = _client(tmp_path)
    try:
        def chunks():
            yield b'{"name": "'
            for _ in range(MAX_BODY_BYTES // 65536 + 2):
                yield b"a" * 65536
            yield b'"}'

        _contract(client.post("/api/workflows", content=chunks(),
                              headers={"Content-Type": "application/json"}), 413, "validation")
        assert client.get("/api/workflows").status_code == 200  # server still healthy
    finally:
        rt.close()


def test_deeply_nested_json_is_a_validation_error_not_500(tmp_path):
    rt, client = _client(tmp_path)
    try:
        depth = 200_000
        payload = '{"name": "n", "config": ' + "[" * depth + "]" * depth + "}"
        assert len(payload) < MAX_BODY_BYTES
        _contract(client.post("/api/workflows", content=payload,
                              headers={"Content-Type": "application/json"}), 400, "validation")
    finally:
        rt.close()


def test_normal_sized_body_still_accepted(tmp_path):
    rt, client = _client(tmp_path)
    try:
        resp = client.post("/api/workflows", json={"name": "ok", "config": {"a": "b" * 1000}})
        assert resp.status_code == 201
    finally:
        rt.close()
