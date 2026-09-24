"""Phase 5 workstream C: runner supervision + runtime loop + CLI. Deterministic: injected clock,
fake sleep, threads only with Event/Barrier."""

import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from storyflow import queue
from storyflow.agents import FakeRunner
from storyflow.artifacts import ArtifactStore
from storyflow.database import Base
from storyflow.gateway import FakeAionGateway, GatewayError
from storyflow.models import (
    AudioChunk, AudioGeneration, ChannelWorkflow, ChannelWorkflowStatus, JobStatus, PipelineJob,
    RunnerInstance, RunnerState, SourceSnapshot, StoryGeneration, StoryProject, TTSGeneration, WorkflowSession,
)
from storyflow.roles import Role
from storyflow.runtime import (
    GatewayRunnerProvider, StaticRunnerProvider, build_runtime, deterministic_fake_providers, ensure_schema,
)
from storyflow.runtime.__main__ import main as cli_main
from storyflow.runtime.app import PipelineRouter, SchemaError
from storyflow.subtitles import FakeSubtitleClient

BACKEND = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "The hero wakes.", "start": 0.0}, {"text": "The rival waits.", "start": 2.0}]}
ROLES = [Role.STORY_WRITER.value, Role.TTS_ADAPTER.value]
FRESH = {"execution_options": {"populate_existing": True}}


class Sleeper:
    """Fake sleep: records durations, never waits."""

    def __init__(self):
        self.calls = []
        self.hook = None

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.hook:
            self.hook(seconds)


class World:
    """One 'process': its own engine, registry, dispatcher, orchestrator, supervisor, runtime."""

    def __init__(self, tmp_path, *, providers=None, clock=None, db_name="rt.db", **kw):
        self.clock = clock or [NOW]
        self.sleeper = Sleeper()
        self.store = ArtifactStore(tmp_path / "artifacts")
        if providers is None:
            providers = deterministic_fake_providers(self.store)
        self.providers = providers
        kw.setdefault("sleep", self.sleeper)
        self.app = build_runtime(
            database_url=f"sqlite:///{(tmp_path / db_name).as_posix()}", artifact_root=tmp_path / "artifacts",
            providers=providers, subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}),
            clock=lambda: self.clock[0], **kw)
        Base.metadata.create_all(self.app.engine)
        self.sf = self.app.session_factory
        self.rt = self.app.runtime

    def q(self, fn):
        db = self.sf()
        try:
            return fn(db)
        finally:
            db.close()

    def session(self):
        def make(db):
            s = WorkflowSession(mode="auto", status="active", role_preferences={})
            db.add(s)
            db.commit()
            return s.id
        return self.q(make)

    def workflow(self, session_id, *, status=ChannelWorkflowStatus.ACTIVE, name="wf"):
        def make(db):
            wf = ChannelWorkflow(workflow_session_id=session_id, name=name, mode="auto", status=status.value,
                                 config={"source": {"video_id": "vid", "languages": ["en"]},
                                         "story": {"branch": "darker"}, "tts": {"voice": "narrator"}})
            db.add(wf)
            db.commit()
            db.add(StoryProject(channel_workflow_id=wf.id, title=name, slug=f"slug-{wf.id}"))
            db.commit()
            return wf.id
        return self.q(make)

    def runner(self, runner_type="fake", external_id="fake-1"):
        return self.q(lambda db: db.scalar(select(RunnerInstance).where(
            RunnerInstance.runner_type == runner_type, RunnerInstance.external_id == external_id), **FRESH))

    def assign(self, session_id, runner_type="fake", external_id="fake-1"):
        def do(db):
            db.execute(update(RunnerInstance).where(RunnerInstance.runner_type == runner_type,
                                                    RunnerInstance.external_id == external_id)
                       .values(workflow_session_id=session_id))
            db.commit()
        self.q(do)

    def set_runner(self, runner_type="fake", external_id="fake-1", **values):
        def do(db):
            db.execute(update(RunnerInstance).where(RunnerInstance.runner_type == runner_type,
                                                    RunnerInstance.external_id == external_id).values(**values))
            db.commit()
        self.q(do)

    def wf_status(self, wid):
        return self.q(lambda db: db.get(ChannelWorkflow, wid, populate_existing=True).status)

    def count(self, model, *where):
        return self.q(lambda db: db.scalar(select(func.count()).select_from(model).where(*where)))

    def jobs(self):
        return self.q(lambda db: db.scalars(select(PipelineJob).order_by(PipelineJob.created_at), **FRESH).all())

    def close(self):
        self.app.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def assert_exactly_one_of_each(w, workflows=1):
    assert w.count(StoryGeneration) == workflows
    assert w.count(TTSGeneration) == workflows
    assert w.count(AudioGeneration) == workflows
    assert w.count(PipelineJob) == 4 * workflows
    dupes = w.q(lambda db: db.execute(
        select(AudioChunk.audio_generation_id, AudioChunk.chunk_index, func.count())
        .group_by(AudioChunk.audio_generation_id, AudioChunk.chunk_index)
        .having(func.count() > 1)).all())
    assert dupes == []
    assert w.count(AudioChunk) >= 2 * workflows


# --- discovery ----------------------------------------------------------------


def test_discovery_creates_unassigned_rows_and_grants_nothing(world):
    sid = world.session()
    wid = world.workflow(sid)
    report = world.rt.run_once()
    row = world.runner()
    assert row is not None and row.workflow_session_id is None and row.state == RunnerState.READY.value
    assert row.supported_roles == ROLES and len(report.refresh.created) == 1
    assert world.app.registry.has(row.id)
    # unassigned runner gets zero jobs: workflow's jobs park, nothing is executed
    for _ in range(3):
        world.rt.run_once()
    assert world.wf_status(wid) == ChannelWorkflowStatus.ACTIVE.value
    assert all(j.execution_count == 0 for j in world.jobs())
    assert world.count(AudioChunk) == 0
    # explicit assignment (done here directly) is what enables work
    world.assign(sid)
    world.rt.run_forever(max_iterations=10)
    assert world.wf_status(wid) == ChannelWorkflowStatus.FINISHED.value


def test_rediscovery_keeps_session_and_operator_roles(world):
    sid = world.session()
    world.rt.run_once()
    world.assign(sid)
    world.set_runner(supported_roles=[Role.REVIEWER.value])
    world.rt.run_once()
    world.rt.run_once()
    row = world.runner()
    assert row.workflow_session_id == sid and row.supported_roles == [Role.REVIEWER.value]
    assert world.count(RunnerInstance) == 1


def test_gateway_provider_discovery_and_health(tmp_path):
    gw = FakeAionGateway(runners={"a": True, "b": False})
    prov = GatewayRunnerProvider(gw, name="aion", roles=[Role.STORY_WRITER.value], max_concurrency=2)
    w = World(tmp_path, providers=[prov])
    try:
        w.rt.run_once()
        a, b = w.runner("aion", "a"), w.runner("aion", "b")
        assert (a.state, b.state) == ("ready", "offline") and a.max_concurrency == 2
        assert a.workflow_session_id is None and a.supported_roles == [Role.STORY_WRITER.value]
        gw.runners["b"] = True
        w.rt.run_once()
        assert w.runner("aion", "b").state == "ready"
    finally:
        w.close()


def test_repeated_and_concurrent_refresh_no_duplicates(tmp_path):
    a, b = World(tmp_path), World(tmp_path)  # two processes, one DB file
    try:
        for _ in range(3):
            a.app.supervisor.refresh()
        assert a.count(RunnerInstance) == 1
        # fresh DB race: drop the row so both threads try to CREATE it
        a.q(lambda db: (db.query(RunnerInstance).delete(), db.commit()))
        barrier = threading.Barrier(2)
        errors = []

        def go(w):
            try:
                barrier.wait(timeout=10)
                w.app.supervisor.refresh()
            except Exception as exc:  # noqa: BLE001 - surface any thread failure to the assertion
                errors.append(exc)
        threads = [threading.Thread(target=go, args=(w,)) for w in (a, b)]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
        assert errors == []
        assert a.count(RunnerInstance) == 1
        rid = a.runner().id
        assert a.app.registry.has(rid) and b.app.registry.has(rid)
    finally:
        a.close()
        b.close()


def test_restart_rebuilds_registry_from_db(tmp_path):
    first = World(tmp_path)
    first.rt.run_once()
    rid = first.runner().id
    first.close()
    second = World(tmp_path)
    try:
        assert not second.app.registry.has(rid)
        second.app.supervisor.refresh()
        assert second.app.registry.has(rid) and second.count(RunnerInstance) == 1
    finally:
        second.close()


def test_restart_rebuilds_registry_even_when_detect_fails(tmp_path):
    first = World(tmp_path)
    first.rt.run_once()
    rid = first.runner().id
    first.close()
    second = World(tmp_path)
    try:
        second.providers[0].fail_detect = TimeoutError("gateway down")
        rep = second.app.supervisor.refresh()
        assert second.app.registry.has(rid)
        assert rep.errors and rep.errors[0]["error"] == "TimeoutError"
        assert second.runner().state == "ready"  # unknown health is not bad health
    finally:
        second.close()


# --- health -------------------------------------------------------------------


def test_health_offline_online_preserves_windows_and_protected_states(world):
    prov = world.providers[0]
    sup = world.app.supervisor
    sup.refresh()
    # plain offline -> ready
    prov.set_health("fake-1", False)
    rep = sup.refresh()
    assert world.runner().state == "offline" and len(rep.went_offline) == 1
    prov.set_health("fake-1", True)
    rep = sup.refresh()
    assert world.runner().state == "ready" and len(rep.restored) == 1

    # quota window still in the future survives offline/online
    future = NOW + timedelta(hours=1)
    world.set_runner(state="quota_exhausted", quota_reset_at=future)
    sup.refresh()  # healthy: must not clear
    assert world.runner().state == "quota_exhausted"
    prov.set_health("fake-1", False)
    sup.refresh()
    r = world.runner()
    assert r.state == "offline" and r.quota_reset_at == future
    prov.set_health("fake-1", True)
    sup.refresh()
    r = world.runner()
    assert r.state == "quota_exhausted" and r.quota_reset_at == future
    # window elapsed while offline -> ready
    prov.set_health("fake-1", False)
    sup.refresh()
    world.clock[0] = future + timedelta(seconds=1)
    prov.set_health("fake-1", True)
    sup.refresh()
    assert world.runner().state == "ready"

    # cooldown window
    cool = world.clock[0] + timedelta(minutes=5)
    world.set_runner(state="offline", cooldown_until=cool, quota_reset_at=None)
    sup.refresh()
    r = world.runner()
    assert r.state == "cooldown" and r.cooldown_until == cool

    # protected states are never touched, healthy or not
    for state in ("auth_error", "disabled"):
        world.set_runner(state=state)
        for ok in (False, True):
            prov.set_health("fake-1", ok)
            sup.refresh()
            assert world.runner().state == state
    world.set_runner(state="rate_limited", cooldown_until=cool)
    prov.set_health("fake-1", True)
    sup.refresh()
    assert world.runner().state == "rate_limited"


def test_offline_runner_is_never_dispatched(world):
    sid = world.session()
    wid = world.workflow(sid)
    world.rt.run_once()
    world.assign(sid)
    world.providers[0].set_health("fake-1", False)
    for _ in range(4):
        world.rt.run_once()
    assert world.runner().state == "offline"
    assert all(j.execution_count == 0 for j in world.jobs())
    assert world.wf_status(wid) == ChannelWorkflowStatus.ACTIVE.value
    world.providers[0].set_health("fake-1", True)
    world.rt.run_forever(max_iterations=10)
    assert world.wf_status(wid) == ChannelWorkflowStatus.FINISHED.value


def test_provider_error_is_reported_not_fatal(tmp_path, caplog):
    class Boom(StaticRunnerProvider):
        def detect(self):
            raise GatewayError("x" * 1000)

    store = ArtifactStore(tmp_path / "artifacts")
    providers = [Boom({}, name="boom"), *deterministic_fake_providers(store)]
    w = World(tmp_path, providers=providers)
    try:
        with caplog.at_level(logging.WARNING):
            rep = w.rt.run_once()
        assert w.runner() is not None  # healthy provider still refreshed
        assert len(rep.refresh.errors) == 1
        err = rep.refresh.errors[0]
        assert err["provider"] == "boom" and err["error"] == "GatewayError" and len(err["message"]) <= 200
        assert any("boom" in r.getMessage() for r in caplog.records)
    finally:
        w.close()


# --- loop ---------------------------------------------------------------------


def test_run_once_only_touches_active_with_session_and_is_bounded(world):
    sid = world.session()
    world.rt.run_once()
    world.assign(sid)
    untouched = [
        world.workflow(sid, status=ChannelWorkflowStatus.DRAFT, name="d"),
        world.workflow(sid, status=ChannelWorkflowStatus.PAUSED, name="p"),
        world.workflow(sid, status=ChannelWorkflowStatus.CANCELLED, name="c"),
        world.workflow(sid, status=ChannelWorkflowStatus.FINISHED, name="f"),
        world.workflow(None, name="nosession"),
    ]
    active = [world.workflow(sid, name=f"a{i}") for i in range(3)]
    world.rt.max_workflows_per_iteration = 2
    world.rt.max_rounds_per_workflow = 1
    rep = world.rt.run_once()
    assert len(rep.workflows) == 2 and all(w.workflow_id in active for w in rep.workflows)
    assert all(w.rounds == 1 for w in rep.workflows)
    # round robin: the third (least recently served) goes first next iteration
    rep2 = world.rt.run_once()
    assert rep2.workflows[0].workflow_id == active[2]
    assert {world.wf_status(w) for w in untouched} == {"draft", "paused", "cancelled", "finished", "active"}
    # inline source step ran only for projects of ACTIVE workflows
    touched = world.q(lambda db: set(db.scalars(
        select(StoryProject.channel_workflow_id).join(SourceSnapshot, SourceSnapshot.story_project_id == StoryProject.id))))
    assert touched and touched <= set(active)


def test_run_once_round_bound_and_early_stop(world):
    sid = world.session()
    world.rt.run_once()
    world.assign(sid)
    world.workflow(sid)
    world.rt.max_rounds_per_workflow = 2
    rep = world.rt.run_once()
    assert rep.workflows[0].rounds == 2 and rep.progressed
    world.rt.max_rounds_per_workflow = 50
    while True:  # runs to completion, then a finished workflow is no longer picked
        rep = world.rt.run_once()
        if not rep.workflows:
            break
    world.workflow(sid, status=ChannelWorkflowStatus.PAUSED)
    rep = world.rt.run_once()
    assert rep.workflows == [] and not rep.progressed


def test_run_forever_backoff_sequence_and_reset(world):
    sid = world.session()
    world.rt.idle_min, world.rt.idle_max = 0.1, 0.5
    rep = world.rt.run_forever(max_iterations=6)  # nothing to do: idle every time
    assert world.sleeper.calls == [0.1, 0.2, 0.4, 0.5, 0.5]  # no sleep after the last iteration
    assert rep.iterations == 6 and rep.stop_reason == "max_iterations" and rep.progressed_iterations == 0
    # new work resets the backoff
    world.assign(sid)  # discovered by the earlier refresh
    world.workflow(sid)
    world.sleeper.calls.clear()
    rep = world.rt.run_forever(max_iterations=8)
    assert rep.progressed_iterations >= 1
    assert world.sleeper.calls[0] == 0.1  # reset after progress
    assert world.sleeper.calls == sorted(world.sleeper.calls)
    assert max(world.sleeper.calls) <= 0.5


def test_graceful_stop_from_fake_sleep_then_new_runtime_finishes(tmp_path):
    w = World(tmp_path)
    try:
        sid = w.session()
        wid = w.workflow(sid)  # runner not assigned yet: jobs park, loop goes idle
        w.sleeper.hook = lambda _s: w.rt.request_stop()
        rep = w.rt.run_forever(max_iterations=50)
        assert rep.stop_reason == "stop_requested" and w.sleeper.calls == [w.rt.idle_min]
        assert w.wf_status(wid) == "active"
        w.assign(sid)
        w2 = World(tmp_path)
        try:
            w2.rt.run_forever(max_iterations=10)
            assert w2.wf_status(wid) == "finished"
            assert_exactly_one_of_each(w2)
        finally:
            w2.close()
    finally:
        w.close()


class GateRunner(PipelineRouter):
    """Signals on its first execute, then blocks until released (Events only)."""

    def __init__(self, store):
        super().__init__(store, runner_type="gate")
        self.started, self.release, self.first = threading.Event(), threading.Event(), True

    def execute(self, packet):
        if self.first:
            self.first = False
            self.started.set()
            assert self.release.wait(10)
        return super().execute(packet)


def test_graceful_stop_from_another_thread_mid_round_then_new_runtime_finishes(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    gate = GateRunner(store)
    prov = StaticRunnerProvider({"g": gate}, name="gate", roles=ROLES)
    w = World(tmp_path, providers=[prov])
    try:
        sid = w.session()
        wid = w.workflow(sid)
        w.rt.run_once()
        w.assign(sid, "gate", "g")

        def stopper():
            assert gate.started.wait(10)
            w.rt.request_stop()
            gate.release.set()
        t = threading.Thread(target=stopper)
        t.start()
        rep = w.rt.run_forever()
        t.join(10)
        assert rep.stop_reason == "stop_requested"
        statuses = {j.status for j in w.jobs()}
        assert JobStatus.PROCESSING.value not in statuses  # round finished atomically, nothing left claimed
        assert JobStatus.COMPLETED.value in statuses
        assert w.wf_status(wid) == "active"
        assert w.count(RunnerInstance, RunnerInstance.active_count != 0) == 0
    finally:
        w.close()
    w2 = World(tmp_path, providers=[StaticRunnerProvider({"g": PipelineRouter(store)}, name="gate", roles=ROLES)])
    try:
        w2.rt.run_forever(max_iterations=10)
        assert w2.wf_status(wid) == "finished"
        assert_exactly_one_of_each(w2)
    finally:
        w2.close()


def test_bad_workflow_is_isolated_reported_and_logged(tmp_path, caplog, monkeypatch):
    store = ArtifactStore(tmp_path / "artifacts")
    providers = [*deterministic_fake_providers(store),
                 StaticRunnerProvider({"x": PipelineRouter(store)}, name="fake2", roles=ROLES)]
    w = World(tmp_path, providers=providers)
    try:
        s1, s2 = w.session(), w.session()
        bad, good = w.workflow(s1, name="bad"), w.workflow(s2, name="good")
        w.rt.run_once()
        w.assign(s1)
        w.assign(s2, "fake2", "x")
        orig = w.app.orchestrator.run_round

        def flaky(wid, **kw):
            if wid == bad:
                raise RuntimeError("boom " + "y" * 1000)
            return orig(wid, **kw)
        monkeypatch.setattr(w.app.orchestrator, "run_round", flaky)
        with caplog.at_level(logging.ERROR):
            reports = [w.rt.run_once() for _ in range(3)]
        assert [r.errors[0]["consecutive_failures"] for r in reports] == [1, 2, 3]
        err = reports[0].errors[0]
        assert err["workflow_id"] == bad and err["error"] == "RuntimeError" and len(err["message"]) <= 200
        assert w.wf_status(good) == "finished" and w.wf_status(bad) == "active"
        assert any(r.exc_info and r.levelno == logging.ERROR for r in caplog.records)
        # a KeyboardInterrupt-style BaseException is NOT swallowed
        monkeypatch.setattr(w.app.orchestrator, "run_round", lambda wid, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            w.rt.run_once()
    finally:
        w.close()


def test_stale_lease_is_recovered_through_the_runtime(world):
    sid = world.session()
    world.rt.run_once()
    world.assign(sid)
    wid = world.workflow(sid)
    world.app.orchestrator.tick(wid)  # enqueue canon job
    row = world.runner()
    db = world.sf()
    try:
        claimed = queue.claim_next_job(db, row.id, 60, runner=row, role=Role.STORY_WRITER.value, now=NOW)
    finally:
        db.close()
    assert claimed is not None  # a dead worker holds it (and the runner's only slot)
    assert world.rt.run_once().workflows[0].rounds >= 1
    assert world.jobs()[0].status == "processing"  # lease still valid: not touched
    world.clock[0] = NOW + timedelta(seconds=300)
    world.rt.run_forever(max_iterations=10)
    assert world.wf_status(wid) == "finished"
    first = world.jobs()[0]
    assert first.infrastructure_failures == 1 and first.status == "completed"
    assert_exactly_one_of_each(world)
    assert world.runner().active_count == 0


def test_full_fake_pipeline_via_run_forever_and_session_isolation(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    foreign = FakeRunner("foreign", roles=ROLES)
    providers = [*deterministic_fake_providers(store),
                 StaticRunnerProvider({"f": foreign}, name="foreign", roles=ROLES)]
    w = World(tmp_path, providers=providers)
    try:
        sid, other = w.session(), w.session()
        wid = w.workflow(sid)
        w.rt.run_once()
        w.assign(sid)
        w.assign(other, "foreign", "f")
        rep = w.rt.run_forever(max_iterations=12)
        assert w.wf_status(wid) == "finished" and rep.errors == 0
        assert_exactly_one_of_each(w)
        assert foreign.invocations == []
        assert all(j.workflow_session_id == sid and j.status == "completed" for j in w.jobs())
    finally:
        w.close()


def test_two_independent_runtimes_same_db_no_duplicates(tmp_path):
    a, b = World(tmp_path), World(tmp_path)
    try:
        sid = a.session()
        wid = a.workflow(sid)
        a.rt.run_once()
        a.assign(sid)
        barrier = threading.Barrier(2)
        errors = []

        def go(w):
            try:
                barrier.wait(timeout=10)
                w.rt.run_forever(max_iterations=25)
            except Exception as exc:  # noqa: BLE001 - surface thread failure
                errors.append(exc)
        threads = [threading.Thread(target=go, args=(w,)) for w in (a, b)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        assert errors == []
        assert a.wf_status(wid) == "finished"
        assert_exactly_one_of_each(a)
        assert a.count(RunnerInstance) == 1
    finally:
        a.close()
        b.close()


# --- schema + CLI -------------------------------------------------------------


def test_ensure_schema_creates_empty_and_refuses_foreign(tmp_path):
    url = f"sqlite:///{(tmp_path / 'new.db').as_posix()}"
    assert ensure_schema(url) == "created"
    assert ensure_schema(url) == "current"
    # tables but no alembic_version (create_all DB): refuse, do not modify
    other = tmp_path / "manual.db"
    w = World(tmp_path, db_name="manual.db")
    try:
        with pytest.raises(SchemaError, match="alembic_version"):
            ensure_schema(f"sqlite:///{other.as_posix()}")
    finally:
        w.close()


def test_cli_once_fake_in_process(tmp_path, caplog):
    db = tmp_path / "cli.db"
    with caplog.at_level(logging.INFO, logger="storyflow.runtime"):
        code = cli_main(["--once", "--fake", "--database-url", f"sqlite:///{db.as_posix()}",
                         "--artifact-root", str(tmp_path / "art")])
    assert code == 0 and db.exists()
    assert any('"iteration": 1' in r.getMessage() for r in caplog.records)
    # --run with a bounded iteration count returns cleanly and does not leak signal handlers
    import signal
    before = signal.getsignal(signal.SIGINT)
    assert cli_main(["--run", "--fake", "--max-iterations", "1", "--database-url", f"sqlite:///{db.as_posix()}",
                     "--artifact-root", str(tmp_path / "art")]) == 0
    assert signal.getsignal(signal.SIGINT) == before


def test_cli_startup_failure_is_actionable(tmp_path, capsys):
    db = tmp_path / "manual.db"
    w = World(tmp_path, db_name="manual.db")  # create_all DB without alembic_version
    w.close()
    assert cli_main(["--once", "--fake", "--database-url", f"sqlite:///{db.as_posix()}"]) == 2
    assert "startup failed" in capsys.readouterr().err


def test_cli_subprocess_smoke(tmp_path):
    db = tmp_path / "sub.db"
    proc = subprocess.run(
        [sys.executable, "-m", "storyflow.runtime", "--once", "--fake", "--database-url",
         f"sqlite:///{db.as_posix()}", "--artifact-root", str(tmp_path / "art")],
        cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert db.exists()
