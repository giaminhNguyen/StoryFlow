"""Phase 6 API core tests: real alembic-migrated SQLite, deterministic fake runners, starlette TestClient.

Every response body is scanned (recursively) for claim tokens, worker ids, leases, absolute paths, the tmp
root and DB urls. No sleeps: the embedded-runtime test waits on RuntimeHost.wait_iteration.
"""

import re
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select, update
from starlette.testclient import TestClient

from storyflow.agents import AgentRunner
from storyflow.api import __main__ as api_main
from storyflow.api.app import create_app
from storyflow.artifacts import ArtifactStore
from storyflow.models import PipelineJob, RunnerInstance, ChannelWorkflow
from storyflow.roles import Role
from storyflow.runtime import StaticRunnerProvider, build_runtime
from storyflow.runtime.app import PipelineRouter
from storyflow.subtitles import FakeSubtitleClient

NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "The hero wakes.", "start": 0.0}, {"text": "The rival waits.", "start": 2.0}]}
ROLES = [Role.STORY_WRITER.value, Role.TTS_ADAPTER.value]
CONFIG = {"source": {"video_id": "vid", "languages": ["en"]}, "story": {"branch": "a darker turn"},
          "tts": {"voice": "narrator"}}
FORBIDDEN_KEYS = {"claim_token", "worker_id", "lease_expires_at", "lease_until", "leased_by", "database_url"}
ABS_PATH = re.compile(r"[A-Za-z]:[\\/]|(?<![\w.:])/(?:home|Users|tmp|var|etc|usr|mnt|root|opt|private)/")
ROOTS: list[str] = []


def scan(node, where="body"):
    if isinstance(node, dict):
        for k, v in node.items():
            assert k not in FORBIDDEN_KEYS and "claim_token" not in k and "worker_id" not in k, f"{where}.{k}"
            scan(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            scan(v, f"{where}[{i}]")
    elif isinstance(node, str):
        assert not ABS_PATH.search(node), f"absolute path in {where}: {node[:80]}"
        assert "sqlite:" not in node, f"db url in {where}"
        for root in ROOTS:
            assert root not in node, f"tmp root leaked in {where}"


class ScanClient(TestClient):
    def request(self, *args, **kwargs):
        resp = super().request(*args, **kwargs)
        if "json" in resp.headers.get("content-type", ""):
            scan(resp.json(), f"{args[0]} {args[1]}")
        return resp


class BlockingRunner(AgentRunner):
    def __init__(self, inner):
        self.inner = inner
        self.runner_type = inner.runner_type
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, packet):
        if packet.task_config["step"] == "canon":
            self.started.set()
            assert self.release.wait(timeout=30), "test never released the runner"
        return self.inner.execute(packet)

    def classify_error(self, error):
        return self.inner.classify_error(error)


def make_runtime(tmp_path, *, router=None, first=True, **kwargs):
    ROOTS[:] = [str(tmp_path), tmp_path.as_posix()]
    root = tmp_path / "artifacts"
    router = router or PipelineRouter(ArtifactStore(root))
    provider = StaticRunnerProvider({"r1": router}, name="fake", roles=ROLES)
    rt = build_runtime(database_url=f"sqlite:///{(tmp_path / 'storyflow.db').as_posix()}", artifact_root=root,
                       providers=[provider], clock=lambda: NOW,
                       subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}),
                       ensure_db_schema=first, **kwargs)
    rt.test_router = router
    return rt


@pytest.fixture
def rt(tmp_path):
    app = make_runtime(tmp_path, sleep=lambda s: None)
    yield app
    app.close()


@pytest.fixture
def client(rt):
    return ScanClient(create_app(rt))


def count(rt, model, *where):
    with rt.session_factory() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def new_workflow(client, name="chan", project=True, **body):
    r = client.post("/api/workflows", json={"name": name, "config": CONFIG, **body})
    assert r.status_code == 201, r.text
    wf = r.json()["workflow"]["id"]
    if project:
        assert client.post(f"/api/workflows/{wf}/projects", json={"title": "Tale"}).status_code == 201
    return wf


def discover_and_assign(rt, client, wf):
    rt.supervisor.refresh()
    (runner,) = client.get("/api/runners", params={"unassigned": "true"}).json()["runners"]
    r = client.post(f"/api/runners/{runner['id']}/assign", json={"workflow_id": wf})
    assert r.status_code == 200, r.text
    return runner["id"]


def drive(rt, client, wf, until, tries=60):
    for _ in range(tries):
        rt.runtime.run_once()
        snap = client.get(f"/api/workflows/{wf}").json()
        if until(snap):
            return snap
    raise AssertionError(f"condition not reached: {snap['display_state']}")


def assert_error(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert set(body["error"]) == {"code", "message", "details"}
    return body["error"]


# ---------------------------------------------------------------- health


def test_health_shape_and_no_secrets(client, tmp_path):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"]["ok"] and body["db"]["at_head"] and body["db"]["schema_revision"]
    assert body["runtime"] == {"mode": "external", "running": None, "iteration": None, "errors": None}
    assert body["runners"] == {"registered": 0, "assigned": 0, "ready": 0, "offline": 0}
    assert body["version"]
    assert str(tmp_path) not in r.text and "sqlite" not in r.text and "storyflow.db" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


def test_health_counts_runners_after_discovery(rt, client):
    rt.supervisor.refresh()
    assert client.get("/api/health").json()["runners"] == {"registered": 1, "assigned": 0, "ready": 1, "offline": 0}


def test_health_degraded_503_when_db_broken(rt, client, tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "engine", create_engine(f"sqlite:///{(tmp_path / 'missing' / 'x.db').as_posix()}"))
    r = client.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded" and body["db"] == {"ok": False, "schema_revision": None, "at_head": False}
    assert set(body) == {"status", "db", "runtime", "runners", "version"}
    assert "missing" not in r.text


# ---------------------------------------------------------------- workflows


def test_create_list_get_workflow(client):
    wf = new_workflow(client, "First")
    listed = client.get("/api/workflows").json()["workflows"]
    assert [w["id"] for w in listed] == [wf] and listed[0]["display_state"] == "draft"
    snap = client.get(f"/api/workflows/{wf}").json()
    assert snap["name"] == "First" and snap["status"] == "draft" and snap["project_count"] == 1
    assert client.get("/api/workflows", params={"status": "active"}).json()["workflows"] == []
    assert client.get("/api/workflows", params={"limit": 0}).json()["workflows"] == []
    assert_error(client.get("/api/workflows", params={"status": "bogus"}), 422, "validation")
    assert_error(client.get("/api/workflows", params={"limit": 501}), 422, "validation")
    assert_error(client.get("/api/workflows", params={"offset": -1}), 422, "validation")
    assert_error(client.get("/api/workflows/nosuchid"), 404, "not_found")
    assert_error(client.post("/api/workflows", json={"name": "   "}), 422, "validation")


def test_idempotency_key_and_client_key_replay(rt, client):
    first = client.post("/api/workflows", json={"name": "a"}, headers={"Idempotency-Key": "k-1"})
    again = client.post("/api/workflows", json={"name": "a"}, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["workflow"]["id"] == again.json()["workflow"]["id"]
    assert again.json()["result"]["changed"] is False
    body = client.post("/api/workflows", json={"name": "b", "client_key": "k-2"})
    body2 = client.post("/api/workflows", json={"name": "b", "client_key": "k-2"}, headers={"Idempotency-Key": "zzz"})
    assert (body.status_code, body2.status_code) == (201, 200)
    assert body.json()["workflow"]["id"] == body2.json()["workflow"]["id"]
    assert count(rt, ChannelWorkflow) == 2


def test_concurrent_idempotent_creates_make_one_row(rt):
    app = create_app(rt)
    barrier = threading.Barrier(6)
    out = []

    def go():
        c = ScanClient(app)
        barrier.wait(timeout=30)
        out.append(c.post("/api/workflows", json={"name": "race"}, headers={"Idempotency-Key": "same"}))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(out) == 6 and all(r.status_code in (200, 201) for r in out)
    assert len({r.json()["workflow"]["id"] for r in out}) == 1
    assert sum(r.status_code == 201 for r in out) == 1
    assert count(rt, ChannelWorkflow) == 1


def test_add_project_slug_idempotency_and_conflict(client):
    wf = new_workflow(client, project=False)
    other = new_workflow(client, "other", project=False)
    r = client.post(f"/api/workflows/{wf}/projects", json={"title": "Tale", "slug": "tale", "description": "d"})
    assert r.status_code == 201 and r.json()["project"]["slug"] == "tale" and r.json()["result"]["changed"]
    pid = r.json()["project"]["id"]
    replay = client.post(f"/api/workflows/{wf}/projects", json={"title": "Tale", "slug": "tale"})
    assert replay.status_code == 200 and replay.json()["project"]["id"] == pid
    err = assert_error(client.post(f"/api/workflows/{other}/projects", json={"title": "X", "slug": "tale"}),
                       409, "conflict")
    assert err["details"]["reason"] == "slug_taken"
    got = client.get(f"/api/projects/{pid}").json()
    assert got["id"] == pid and got["workflow_id"] == wf
    assert_error(client.get("/api/projects/missing"), 404, "not_found")
    assert_error(client.post("/api/workflows/missing/projects", json={"title": "x"}), 404, "not_found")
    auto = client.post(f"/api/workflows/{wf}/projects", json={"title": "Same Title"}).json()["project"]["slug"]
    assert client.post(f"/api/workflows/{wf}/projects", json={"title": "Same Title"}).json()["project"]["slug"] != auto


def test_start_empty_workflow_is_invalid_state(client):
    wf = new_workflow(client, project=False)
    err = assert_error(client.post(f"/api/workflows/{wf}/start"), 409, "invalid_state")
    assert err["details"]["reason"] == "no_projects"
    assert_error(client.post("/api/workflows/missing/start"), 404, "not_found")


def test_lifecycle_commands_status_codes_and_bodies(client):
    wf = new_workflow(client)
    r = client.post(f"/api/workflows/{wf}/start")
    assert r.status_code == 200
    assert r.json()["result"]["changed"] is True and r.json()["result"]["status"] == "active"
    assert r.json()["workflow"]["id"] == wf and r.json()["workflow"]["display_state"] in ("active", "waiting_capacity")
    assert client.post(f"/api/workflows/{wf}/start").json()["result"]["changed"] is False
    assert_error(client.post(f"/api/workflows/{wf}/retry"), 409, "not_retryable")

    p = client.post(f"/api/workflows/{wf}/pause").json()
    assert p["result"]["changed"] and p["workflow"]["status"] == "paused" and p["workflow"]["status_reason"] == "operator"
    assert client.post(f"/api/workflows/{wf}/pause").json()["result"]["changed"] is False
    err = assert_error(client.post(f"/api/workflows/{wf}/retry", json={}), 409, "not_retryable")
    assert err["details"]["reason"] == "not_failed"
    assert client.post(f"/api/workflows/{wf}/resume").json()["workflow"]["status"] == "active"
    assert client.post(f"/api/workflows/{wf}/resume").json()["result"]["changed"] is False

    c = client.post(f"/api/workflows/{wf}/cancel").json()
    assert c["result"]["changed"] and c["workflow"]["status"] == "cancelled"
    assert client.post(f"/api/workflows/{wf}/cancel").json()["result"]["changed"] is False
    assert_error(client.post(f"/api/workflows/{wf}/start"), 409, "invalid_state")
    assert_error(client.post(f"/api/workflows/{wf}/retry"), 409, "invalid_state")
    assert_error(client.post(f"/api/workflows/{wf}/projects", json={"title": "late"}), 409, "invalid_state")
    assert_error(client.post(f"/api/workflows/{wf}/retry", json={"project_id": "x", "extra": 1}), 422, "validation")


def test_retry_after_failed_step(rt, client):
    wf = new_workflow(client)
    discover_and_assign(rt, client, wf)
    client.post(f"/api/workflows/{wf}/start")
    rt.test_router.story.emit_invalid = {"story"}
    snap = drive(rt, client, wf, lambda s: s["display_state"] == "failed")
    assert snap["projects"][0]["failure"]["category"] == "business"
    err = assert_error(client.post(f"/api/workflows/{wf}/resume"), 409, "invalid_state")
    assert err["details"]["reason"] == "has_failed_steps"
    assert_error(client.post(f"/api/workflows/{wf}/retry", json={"project_id": "unknown"}), 404, "not_found")
    rt.test_router.story.emit_invalid = False
    r = client.post(f"/api/workflows/{wf}/retry")
    assert r.status_code == 200 and r.json()["result"]["changed"] and r.json()["workflow"]["status"] == "active"
    done = drive(rt, client, wf, lambda s: s["display_state"] == "completed")
    assert done["counts"]["completed"] == 1


def test_cancel_over_http_while_runner_processing_late_result_stays_cancelled(tmp_path):
    router = BlockingRunner(PipelineRouter(ArtifactStore(tmp_path / "artifacts")))
    rt = make_runtime(tmp_path, router=router, sleep=lambda s: None)
    try:
        client = ScanClient(create_app(rt))
        wf = new_workflow(client)
        client.post(f"/api/workflows/{wf}/start")
        discover_and_assign(rt, client, wf)
        worker = threading.Thread(target=rt.runtime.run_once)
        worker.start()
        assert router.started.wait(timeout=30)
        c = client.post(f"/api/workflows/{wf}/cancel")
        assert c.status_code == 200 and c.json()["result"]["changed"] and c.json()["workflow"]["status"] == "cancelled"
        router.release.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
        for _ in range(3):
            rt.runtime.run_once()
        snap = client.get(f"/api/workflows/{wf}").json()
        assert snap["status"] == "cancelled" and snap["display_state"] == "cancelled"
        assert [j.kind for j in _jobs(rt)] == ["canon_analysis"]
        assert client.post(f"/api/workflows/{wf}/cancel").json()["result"]["changed"] is False
    finally:
        router.release.set()
        rt.close()


def _jobs(rt):
    with rt.session_factory() as db:
        return list(db.scalars(select(PipelineJob)))


# ---------------------------------------------------------------- runners


def test_runners_unassigned_by_default_then_assign_unassign_enable_disable(rt, client):
    rt.supervisor.refresh()
    listed = client.get("/api/runners").json()["runners"]
    assert len(listed) == 1 and listed[0]["assigned"] is False
    rid = listed[0]["id"]
    assert client.get("/api/runners", params={"unassigned": "true"}).json()["runners"][0]["id"] == rid
    assert client.get(f"/api/runners/{rid}").json()["id"] == rid
    assert_error(client.get("/api/runners/nope"), 404, "not_found")

    wf = new_workflow(client)
    wf2 = new_workflow(client, "two")
    client.post(f"/api/workflows/{wf}/start")  # starting never assigns a runner implicitly
    assert client.get("/api/runners", params={"unassigned": "true"}).json()["runners"][0]["assigned"] is False

    r = client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf, "roles": ["story_writer"]})
    assert r.status_code == 200 and r.json()["result"]["changed"] and r.json()["runner"]["assigned"]
    assert r.json()["runner"]["supported_roles"] == ["story_writer"]
    assert client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf}).json()["result"]["changed"] is False
    err = assert_error(client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf2}), 409, "conflict")
    assert err["details"]["reason"] == "assigned_to_other_session"
    assert [x["id"] for x in client.get("/api/runners", params={"workflow_id": wf}).json()["runners"]] == [rid]
    assert client.get("/api/runners", params={"workflow_id": wf2}).json()["runners"] == []
    assert_error(client.get("/api/runners", params={"workflow_id": wf, "unassigned": "true"}), 422, "validation")

    assert_error(client.post(f"/api/runners/{rid}/assign", json={}), 422, "validation")
    assert_error(client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf, "session_id": "s"}), 422,
                 "validation")
    assert_error(client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf, "roles": ["hacker"]}), 422,
                 "validation")
    assert_error(client.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf, "roles": []}), 422, "validation")
    assert_error(client.post(f"/api/runners/{rid}/assign", json={"workflow_id": "missing"}), 404, "not_found")
    assert_error(client.post("/api/runners/nope/assign", json={"workflow_id": wf}), 404, "not_found")

    with rt.session_factory() as db:  # simulate an in-flight job
        db.execute(update(RunnerInstance).where(RunnerInstance.id == rid).values(active_count=1))
        db.commit()
    assert assert_error(client.post(f"/api/runners/{rid}/unassign"), 409, "conflict")["details"]["reason"] == "runner_busy"
    with rt.session_factory() as db:
        db.execute(update(RunnerInstance).where(RunnerInstance.id == rid).values(active_count=0))
        db.commit()
    u = client.post(f"/api/runners/{rid}/unassign")
    assert u.status_code == 200 and u.json()["result"]["changed"] and u.json()["runner"]["assigned"] is False
    assert client.post(f"/api/runners/{rid}/unassign").json()["result"]["changed"] is False

    d = client.post(f"/api/runners/{rid}/disable").json()
    assert d["result"]["changed"] and d["runner"]["enabled"] is False and d["runner"]["effective_state"] == "disabled"
    assert client.post(f"/api/runners/{rid}/disable").json()["result"]["changed"] is False
    e = client.post(f"/api/runners/{rid}/enable").json()
    assert e["result"]["changed"] and e["runner"]["enabled"] and e["runner"]["effective_state"] == "ready"
    assert_error(client.get(f"/api/runners/{rid}/enable"), 405, "validation")


def test_session_isolation_runner_of_b_never_runs_a(rt, client):
    a = new_workflow(client, "A")
    b = new_workflow(client, "B")
    discover_and_assign(rt, client, b)
    client.post(f"/api/workflows/{a}/start")
    client.post(f"/api/workflows/{b}/start")
    done_b = drive(rt, client, b, lambda s: s["display_state"] == "completed")
    assert done_b["projects"][0]["audio"]["chunks"]
    snap_a = client.get(f"/api/workflows/{a}").json()
    assert snap_a["display_state"] == "waiting_capacity" and snap_a["counts"]["completed"] == 0
    project_a = snap_a["projects"][0]
    assert project_a["story_version"] is None and project_a["audio"] is None
    with rt.session_factory() as db:
        completed = db.scalars(select(PipelineJob).where(
            PipelineJob.workflow_session_id == snap_a["session_id"], PipelineJob.status == "completed")).all()
    assert completed == []


# ---------------------------------------------------------------- error contract / hardening


def test_invalid_id_shapes_and_unknown_routes(client):
    assert_error(client.get("/api/workflows/a%20b"), 422, "validation")
    assert_error(client.get("/api/workflows/" + "x" * 65), 422, "validation")
    assert_error(client.get("/api/projects/a.b"), 422, "validation")
    assert_error(client.get("/api/runners/a%20b"), 422, "validation")
    assert_error(client.get("/api/workflows/..%2Fetc"), 404, "not_found")
    assert_error(client.get("/api/runners", params={"workflow_id": "a b"}), 422, "validation")
    assert_error(client.get("/api/nope"), 404, "not_found")
    assert_error(client.get("/"), 404, "not_found")
    err = assert_error(client.delete("/api/workflows"), 405, "validation")
    assert "nope" not in err["message"]
    assert_error(client.put("/api/health"), 405, "validation")


def test_validation_errors_do_not_echo_input(client):
    r = client.post("/api/workflows", json={"name": "n", "bogus": "ECHO_ME_PLEASE"})
    assert_error(r, 422, "validation")
    assert "ECHO_ME_PLEASE" not in r.text
    r = client.post("/api/workflows", json={"name": {"nested": "ECHO_ME_PLEASE"}})
    assert_error(r, 422, "validation")
    assert "ECHO_ME_PLEASE" not in r.text
    r = client.post("/api/workflows", content=b'{"name": ECHO_ME_PLEASE', headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "ECHO_ME_PLEASE" not in r.text


@pytest.mark.parametrize("cfg", [
    {"api_key": "sk-ECHO"}, {"a": {"b": [{"Password": "x"}]}}, {"my-Secret-thing": 1}, {"x": {"auth_token": "t"}},
    {"apikey": "k"}, {"provider": {"Credentials": {}}}])
def test_secret_looking_config_keys_are_rejected_and_never_persisted(rt, client, cfg):
    r = client.post("/api/workflows", json={"name": "n", "config": cfg})
    assert_error(r, 422, "validation")
    assert "ECHO" not in r.text and "api_key" not in r.text.lower().replace("apikey", "")
    assert count(rt, ChannelWorkflow) == 0


def test_config_size_and_depth_limits(rt, client):
    big = {"blob": "x" * 70_000}
    assert_error(client.post("/api/workflows", json={"name": "n", "config": big}), 422, "validation")
    deep: dict = {}
    node = deep
    for _ in range(12):
        node["a"] = {}
        node = node["a"]
    assert_error(client.post("/api/workflows", json={"name": "n", "config": deep}), 422, "validation")
    assert_error(client.post("/api/workflows", json={"name": "x" * 129}), 422, "validation")
    assert_error(client.post("/api/workflows", json={"name": "n", "config": []}), 422, "validation")
    assert client.post("/api/workflows", json={"name": "ok", "config": {"nested": {"ok": [1, 2, 3]}}}).status_code == 201
    assert count(rt, ChannelWorkflow) == 1


def test_internal_errors_are_generic(rt, monkeypatch):
    client = ScanClient(create_app(rt), raise_server_exceptions=False)
    wf = new_workflow(client)

    def boom(*_a, **_k):
        raise RuntimeError("boom /secret/path C:\\secret\\dir")

    monkeypatch.setattr(client.app.state.container.read, "get_workflow", boom)
    r = client.get(f"/api/workflows/{wf}")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal", "message": "internal error", "details": {}}}
    assert "boom" not in r.text and "secret" not in r.text and "Traceback" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


def test_cors_and_trusted_host(client):
    ok = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert client.get("/api/health", headers={"Origin": "http://127.0.0.1:3000"}).headers[
        "access-control-allow-origin"] == "http://127.0.0.1:3000"
    for origin in ("http://evil.example", "https://localhost:5173", "http://localhost.evil.example"):
        assert "access-control-allow-origin" not in client.get("/api/health", headers={"Origin": origin}).headers
    pre = client.options("/api/workflows", headers={"Origin": "http://localhost:5173",
                                                    "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": "Content-Type, Idempotency-Key"})
    assert pre.status_code == 200 and "POST" in pre.headers["access-control-allow-methods"]
    bad = client.options("/api/workflows", headers={"Origin": "http://localhost:5173",
                                                    "Access-Control-Request-Method": "DELETE"})
    assert bad.status_code == 400

    foreign = client.get("/api/health", headers={"Host": "evil.example"})
    assert foreign.status_code == 400 and foreign.headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "[::1]:8765"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200


# ---------------------------------------------------------------- CLI


def test_cli_parse_defaults_and_loopback_policy(monkeypatch, tmp_path):
    args = api_main.parse_args([])
    assert args.host == "127.0.0.1" and args.port == 8765 and not args.no_runtime and not args.allow_non_loopback
    for h in ("127.0.0.1", "127.5.5.5", "::1", "[::1]", "localhost"):
        assert api_main.is_loopback_host(h)
    for h in ("0.0.0.0", "192.168.1.5", "example.com", "::"):
        assert not api_main.is_loopback_host(h)

    monkeypatch.setattr(api_main, "build_server_app", lambda a: pytest.fail("must not build for refused host"))
    assert api_main.main(["--host", "0.0.0.0"]) == 2

    class FakeRT:
        closed = False

        def close(self):
            self.closed = True

    calls = {}
    fake_rt = FakeRT()
    monkeypatch.setattr(api_main, "build_server_app", lambda a: ("APP", fake_rt))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    assert api_main.main(["--host", "0.0.0.0", "--allow-non-loopback", "--port", "9001"]) == 0
    assert calls["host"] == "0.0.0.0" and calls["port"] == 9001 and calls["app"] == "APP" and fake_rt.closed


def test_cli_build_server_app_seam(tmp_path):
    args = api_main.parse_args(["--fake", "--no-runtime", "--database-url",
                                f"sqlite:///{(tmp_path / 'cli.db').as_posix()}",
                                "--artifact-root", str(tmp_path / "art")])
    api, runtime_app = api_main.build_server_app(args)
    try:
        assert api.state.container.host is None
        assert TestClient(api).get("/api/health").json()["runtime"]["mode"] == "external"
    finally:
        runtime_app.close()


# ---------------------------------------------------------------- embedded runtime


def _wait(host, cond, tries=400):
    for _ in range(tries):
        if cond():
            return
        assert host.wait_iteration(30), "runtime stopped or stalled"
    raise AssertionError("condition not reached")


def test_embedded_runtime_full_flow_and_resume_on_second_startup(tmp_path):
    rt1 = make_runtime(tmp_path)
    app1 = create_app(rt1, run_runtime=True)
    host1 = app1.state.container.host
    try:
        with ScanClient(app1) as c:
            assert host1.running
            health = c.get("/api/health").json()
            assert health["runtime"]["mode"] == "embedded" and health["runtime"]["running"] is True
            wf = new_workflow(c)
            _wait(host1, lambda: c.get("/api/runners", params={"unassigned": "true"}).json()["runners"])
            rid = c.get("/api/runners", params={"unassigned": "true"}).json()["runners"][0]["id"]
            assert c.post(f"/api/runners/{rid}/assign", json={"workflow_id": wf}).status_code == 200
            assert c.post(f"/api/workflows/{wf}/start").status_code == 200
            _wait(host1, lambda: c.get(f"/api/workflows/{wf}").json()["display_state"] == "completed")
            project = c.get(f"/api/workflows/{wf}").json()["projects"][0]
            assert project["story_version"]["version_number"] == 1
            assert project["audio"]["chunks"] and project["audio"]["chunk_count"] == len(project["audio"]["chunks"])
            assert host1.last_error_count == 0
            wf2 = new_workflow(c, "second")  # left draft for the restart below
            assert c.post(f"/api/workflows/{wf2}/start").status_code == 200
        assert host1.running is False and host1.thread is None and not host1.stop_timed_out
        assert host1.stop() is True  # idempotent
    finally:
        rt1.close()

    rt2 = make_runtime(tmp_path, first=False)
    app2 = create_app(rt2, run_runtime=True)
    host2 = app2.state.container.host
    try:
        with ScanClient(app2) as c:
            _wait(host2, lambda: c.get(f"/api/workflows/{wf}").json()["display_state"] == "completed")
            assert c.get("/api/health").json()["status"] == "ok"
            rid2 = c.get("/api/runners", params={"workflow_id": wf}).json()["runners"]
            assert len(rid2) == 1
            assert c.post(f"/api/runners/{rid2[0]['id']}/unassign").status_code == 200
            assert c.post(f"/api/runners/{rid2[0]['id']}/assign", json={"workflow_id": wf2}).status_code == 200
            _wait(host2, lambda: c.get(f"/api/workflows/{wf2}").json()["display_state"] == "completed")
        assert not host2.running and host2.thread is None
    finally:
        rt2.close()
