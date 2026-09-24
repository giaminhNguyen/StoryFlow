"""Orchestrator tests (Phase 4, workstream 1).

The step handlers below are TEST-ONLY. They persist their state in the REAL domain tables
(SourceSnapshot / CanonAnalysis / StoryGeneration+StoryVersion / TTSGeneration /
AudioGeneration+AudioChunk), so the orchestrator is exercised against durable state only.
Jobs run through the real Phase 2 Dispatcher with FakeRunner. No sleeps; injected clock.
"""

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from storyflow import queue
from storyflow.agents import CRASH, FakeRunner, RunnerRegistry
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.models import (
    AudioChunk,
    AudioGeneration,
    CanonAnalysis,
    ChannelWorkflow,
    JobStatus,
    PipelineJob,
    RunnerInstance,
    SourceSnapshot,
    StoryGeneration,
    StoryProject,
    StoryVersion,
    TTSGeneration,
    WorkflowSession,
)
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import InlineStepHandler, JobSpec, PipelineContext, StepHandler, StepStatus, StepView
from storyflow.protocol import ResultCode, RunnerResult

BASE = datetime(2026, 3, 1, 9, 0, 0)
LIVE = ("queued", "processing", "completed")
ROLES = ["story_writer", "tts_adapter"]
KIND_ORDER = ["canon_analysis", "story_generation", "tts_adaptation", "audio_synthesis"]
FRESH = {"execution_options": {"populate_existing": True}}


def rows(db, stmt):
    return db.scalars(stmt, **FRESH).all()


# --------------------------------------------------------------------- test-only handlers


class SnapshotSource(InlineStepHandler):
    step = "source"

    def __init__(self, fail=False):
        self.fail = fail
        self.runs = 0

    def _snap(self, db, project_id):
        return db.scalar(select(SourceSnapshot).where(
            SourceSnapshot.story_project_id == project_id, SourceSnapshot.status == "active"), **FRESH)

    def status(self, db, ctx, project):
        snap = self._snap(db, project.id)
        return StepView(StepStatus.COMPLETED if snap else StepStatus.NOT_STARTED, domain_id=snap.id if snap else None)

    def run(self, ctx, project_id):
        self.runs += 1
        if self.fail:
            return StepView(StepStatus.FAILED, error_code="subtitles_unavailable")
        db = ctx.session_factory()
        try:
            snap = self._snap(db, project_id)
            if snap is None:
                db.add(SourceSnapshot(story_project_id=project_id, snapshot_number=1, title="t", content="c"))
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                snap = self._snap(db, project_id)
            return StepView(StepStatus.COMPLETED, domain_id=snap.id)
        finally:
            db.close()


class DomainStep(StepHandler):
    model = None

    # subclass hooks
    def rows_for(self, db, project): raise NotImplementedError
    def new_row(self, db, project): raise NotImplementedError
    def after_complete(self, db, row): pass

    def _pick(self, found):
        for want in (("completed",), ("queued", "processing"), ("failed", "cancelled")):
            for r in found:
                if r.status in want:
                    return r
        return None

    def status(self, db, ctx, project):
        row = self._pick(self.rows_for(db, project))
        if row is None:
            return StepView(StepStatus.NOT_STARTED)
        st = {"completed": StepStatus.COMPLETED, "failed": StepStatus.FAILED, "cancelled": StepStatus.FAILED}.get(
            row.status, StepStatus.IN_PROGRESS)
        return StepView(st, row.id, getattr(row, "pipeline_job_id", None), row.error_code)

    def begin(self, db, ctx, project):
        live = [r for r in self.rows_for(db, project) if r.status in LIVE]
        if not live:
            row = self.new_row(db, project)
            if row is None:
                return None
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                live = [r for r in self.rows_for(db, project) if r.status in LIVE]
                row = live[0]
        else:
            row = live[0]
        return row.id, JobSpec(kind=self.job_kind, role=self.role, dedupe_key=f"{self.step}:{row.id}",
                               payload={"task_config": {"step": self.step}, "inputs": {"domain_id": row.id}})

    def link_job(self, db, ctx, domain_id, job):
        db.execute(update(self.model).where(
            self.model.id == domain_id,
            or_(self.model.pipeline_job_id.is_(None), self.model.pipeline_job_id == job.id),
        ).values(pipeline_job_id=job.id))
        db.commit()

    def finalize(self, db, ctx, domain_id, job):
        db.execute(update(self.model).where(
            self.model.id == domain_id, self.model.status.in_(("queued", "processing")),
        ).values(status="completed"))
        db.commit()
        self.after_complete(db, db.scalar(select(self.model).where(self.model.id == domain_id), **FRESH))

    def mark_failed(self, db, ctx, domain_id, job):
        db.execute(update(self.model).where(
            self.model.id == domain_id, self.model.status.in_(("queued", "processing")),
        ).values(status="failed", error_code=job.last_error_code))
        db.commit()


def _snapshot(db, project):
    return db.scalar(select(SourceSnapshot).where(
        SourceSnapshot.story_project_id == project.id, SourceSnapshot.status == "active"), **FRESH)


class CanonStep(DomainStep):
    step, job_kind, role, model = "canon", "canon_analysis", "story_writer", CanonAnalysis

    def rows_for(self, db, project):
        snap = _snapshot(db, project)
        if snap is None:
            return []
        return rows(db, select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snap.id))

    def new_row(self, db, project):
        return CanonAnalysis(source_snapshot_id=_snapshot(db, project).id)


class StepStory(DomainStep):
    step, job_kind, role, model = "story", "story_generation", "story_writer", StoryGeneration

    def _inputs(self, db, project):
        snap = _snapshot(db, project)
        canon = snap and db.scalar(select(CanonAnalysis).where(
            CanonAnalysis.source_snapshot_id == snap.id, CanonAnalysis.status == "completed"), **FRESH)
        return snap, canon

    def rows_for(self, db, project):
        snap, canon = self._inputs(db, project)
        if canon is None:
            return []
        return rows(db, select(StoryGeneration).where(
            StoryGeneration.story_project_id == project.id, StoryGeneration.source_snapshot_id == snap.id,
            StoryGeneration.canon_analysis_id == canon.id))

    def new_row(self, db, project):
        snap, canon = self._inputs(db, project)
        if canon is None:
            return None
        return StoryGeneration(story_project_id=project.id, source_snapshot_id=snap.id, canon_analysis_id=canon.id)

    def after_complete(self, db, row):
        if db.scalar(select(StoryVersion).where(StoryVersion.story_generation_id == row.id)) is None:
            db.add(StoryVersion(story_generation_id=row.id, story_project_id=row.story_project_id,
                                version_number=1, title="v1", content="text"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()


def _version(db, project):
    return db.scalar(select(StoryVersion).where(
        StoryVersion.story_project_id == project.id, StoryVersion.status == "active"), **FRESH)


class TTSStep(DomainStep):
    step, job_kind, role, model = "tts", "tts_adaptation", "tts_adapter", TTSGeneration

    def rows_for(self, db, project):
        v = _version(db, project)
        if v is None:
            return []
        return rows(db, select(TTSGeneration).where(
            TTSGeneration.story_version_id == v.id, TTSGeneration.voice == "v", TTSGeneration.engine == "e"))

    def new_row(self, db, project):
        v = _version(db, project)
        return TTSGeneration(story_version_id=v.id, voice="v", engine="e") if v else None


class AudioStep(DomainStep):
    step, job_kind, role, model = "audio", "audio_synthesis", "tts_adapter", AudioGeneration

    def _tts(self, db, project):
        v = _version(db, project)
        return v and db.scalar(select(TTSGeneration).where(
            TTSGeneration.story_version_id == v.id, TTSGeneration.status == "completed"), **FRESH)

    def rows_for(self, db, project):
        t = self._tts(db, project)
        return rows(db, select(AudioGeneration).where(AudioGeneration.tts_generation_id == t.id)) if t else []

    def new_row(self, db, project):
        t = self._tts(db, project)
        if t is None:
            return None
        n = db.scalar(select(func.max(AudioGeneration.run_number)).where(AudioGeneration.tts_generation_id == t.id))
        return AudioGeneration(tts_generation_id=t.id, run_number=(n or 0) + 1)

    def link_job(self, db, ctx, domain_id, job):
        pass   # AudioGeneration has no pipeline_job_id column: the orchestrator finds the job by dedupe_key

    def after_complete(self, db, row):
        if db.scalar(select(AudioChunk).where(AudioChunk.audio_generation_id == row.id)) is None:
            db.add(AudioChunk(audio_generation_id=row.id, chunk_index=0, artifact_path="a/0.wav"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()


# --------------------------------------------------------------------- environment


class Env:
    def __init__(self, session_factory, *, policy="pause_auto_resume", prefs=None, runners=("fake",)):
        self.sf = session_factory
        self.registry = RunnerRegistry()
        self.fakes = {}
        db = session_factory()
        sess = WorkflowSession(mode="auto", status="active", all_agents_unavailable_policy=policy,
                               role_preferences=prefs or {})
        db.add(sess)
        db.commit()
        self.session_id = sess.id
        wf = ChannelWorkflow(workflow_session_id=sess.id, name="wf", mode="auto")
        db.add(wf)
        db.commit()
        self.wf_id = wf.id
        p = StoryProject(channel_workflow_id=wf.id, title="p1", slug="p1")
        db.add(p)
        db.commit()
        self.project_id = p.id
        db.close()
        self.ctx = PipelineContext(session_factory=session_factory, store=None, subtitle_client=None,
                                   clock=lambda: BASE)
        for name in runners:
            self.add_runner(name)
        self.dispatcher = Dispatcher(self.registry)

    def add_runner(self, name, *, results=None, session_id=None, roles=None):
        db = self.sf()
        inst = RunnerInstance(workflow_session_id=session_id or self.session_id, runner_type=name,
                              max_concurrency=2, supported_roles=roles or ROLES)
        db.add(inst)
        db.commit()
        fake = FakeRunner(name, results=results, roles=ROLES)
        self.registry.register(inst.id, fake)
        self.fakes[name] = fake
        db.close()
        return fake

    def orch(self, source=None):
        return Orchestrator(self.ctx, self.dispatcher, source or SnapshotSource(),
                            [CanonStep(), StepStory(), TTSStep(), AudioStep()])

    # observation helpers (fresh session each call: no stale identity map)
    def q(self, fn):
        db = self.sf()
        try:
            return fn(db)
        finally:
            db.close()

    def jobs(self, kind=None):
        def f(db):
            stmt = select(PipelineJob)   # frozen clock => equal created_at: order by chain position
            if kind:
                stmt = stmt.where(PipelineJob.kind == kind)
            return sorted(rows(db, stmt), key=lambda j: (KIND_ORDER.index(j.kind), j.id))
        return self.q(f)

    def active_jobs(self):
        return [j for j in self.jobs() if j.status in ("queued", "processing", "waiting_capacity")]

    def count(self, model):
        return self.q(lambda db: db.scalar(select(func.count()).select_from(model)))

    def wf_status(self):
        return self.q(lambda db: db.scalar(select(ChannelWorkflow.status).where(ChannelWorkflow.id == self.wf_id), **FRESH))

    def wf(self):
        return self.q(lambda db: db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == self.wf_id), **FRESH))

    def invoked_steps(self):
        steps = []
        for fake in self.fakes.values():
            steps += [(p.job_id, p.task_config["step"]) for p in fake.invocations]
        return steps


@pytest.fixture
def env(session_factory):
    return Env(session_factory)


# --------------------------------------------------------------------- scheduling


def test_initial_scheduling(env):
    orch = env.orch()
    res = orch.tick(env.wf_id, now=BASE)

    assert res.workflow_status == "active"
    assert res.current_steps == {env.project_id: "canon"}
    assert res.projects[env.project_id].status is StepStatus.IN_PROGRESS
    (job,) = env.jobs()
    assert (job.kind, job.role, job.status) == ("canon_analysis", "story_writer", "queued")
    assert job.workflow_session_id == env.session_id
    assert job.dedupe_key.startswith("canon:")
    assert res.jobs_enqueued == [job.id]
    canon = env.q(lambda db: rows(db, select(CanonAnalysis))[0])
    assert canon.pipeline_job_id == job.id, "domain row must be linked to its job"
    assert env.count(SourceSnapshot) == 1
    assert env.fakes["fake"].invocations == [], "tick never dispatches"


def test_next_step_scheduled_in_chain_order_only_after_previous_completes(env):
    orch = env.orch()
    orch.tick(env.wf_id, now=BASE)
    assert env.count(StoryGeneration) == 0, "story must not start before canon completes"

    rnd = orch.run_round(env.wf_id, now=BASE)   # dispatch canon, then tick advances immediately
    assert rnd.outcome is DispatchOutcome.DISPATCHED_SUCCESS
    assert rnd.projects[env.project_id].step == "story"
    assert [j.kind for j in env.jobs()] == ["canon_analysis", "story_generation"]
    assert env.count(TTSGeneration) == 0

    orch.run_round(env.wf_id, now=BASE)
    assert [j.kind for j in env.jobs()] == ["canon_analysis", "story_generation", "tts_adaptation"]
    assert [j.role for j in env.jobs()] == ["story_writer", "story_writer", "tts_adapter"]


def test_full_pipeline_runs_in_order_and_finishes_once(env):
    orch = env.orch()
    out = orch.run_until_idle(env.wf_id, now=BASE)

    assert out.status == "completed" and out.workflow_status == "finished"
    assert [s for _, s in env.invoked_steps()] == ["canon", "story", "tts", "audio"]
    wf = env.wf()
    assert wf.status == "finished" and wf.finished_at is not None
    assert env.count(StoryVersion) == 1 and env.count(AudioChunk) == 1
    assert out.current_steps == {env.project_id: None}
    assert all(j.status == "completed" for j in env.jobs())
    assert all(j.attempts == 0 and j.infrastructure_failures == 0 for j in env.jobs())


# --------------------------------------------------------------------- idempotency / concurrency


def test_repeated_tick_is_idempotent(env):
    orch = env.orch()
    for _ in range(6):
        orch.tick(env.wf_id, now=BASE)
    assert len(env.jobs()) == 1
    assert env.count(CanonAnalysis) == 1 and env.count(SourceSnapshot) == 1


def test_concurrent_ticks_yield_exactly_one_active_job_per_step(env):
    n = 8

    def storm():
        barrier = threading.Barrier(n)
        errors, sources = [], []

        def worker():
            try:
                o = env.orch()   # independent orchestrator + handlers per thread, own sessions
                sources.append(o.source)
                barrier.wait()
                o.tick(env.wf_id, now=BASE)
            except BaseException as exc:  # surfaced below; not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

    storm()
    assert len(env.jobs("canon_analysis")) == 1
    assert env.count(CanonAnalysis) == 1 and env.count(SourceSnapshot) == 1

    # advance one dispatch, storm again: exactly one story job / row
    env.orch().run_round(env.wf_id, now=BASE)
    storm()
    assert len(env.jobs("story_generation")) == 1
    assert env.count(StoryGeneration) == 1
    assert len(env.active_jobs()) == 1
    assert len({j.dedupe_key for j in env.jobs()}) == len(env.jobs())


# --------------------------------------------------------------------- restart / repair


def test_restart_continues_from_exact_step_with_fresh_orchestrator(env):
    first = env.orch()
    first.run_round(env.wf_id, now=BASE)    # canon done, story scheduled
    first.run_round(env.wf_id, now=BASE)    # story done, tts scheduled
    assert [s for _, s in env.invoked_steps()] == ["canon", "story"]
    del first

    second = env.orch()                     # brand-new objects, same DB
    tick = second.tick(env.wf_id, now=BASE)
    assert tick.current_steps == {env.project_id: "tts"}
    assert len(env.jobs("tts_adaptation")) == 1, "resume must not re-enqueue existing work"
    out = second.run_until_idle(env.wf_id, now=BASE)

    assert out.status == "completed"
    assert [s for _, s in env.invoked_steps()] == ["canon", "story", "tts", "audio"], "no step re-ran"
    assert env.count(StoryVersion) == 1


def test_restart_finalizes_job_completed_before_crash(env):
    """Job completed by the dispatcher but the process died before finalize."""
    orch = env.orch()
    orch.tick(env.wf_id, now=BASE)
    session = env.q(lambda db: db.get(WorkflowSession, env.session_id))
    db = env.sf()
    env.dispatcher.run_round(db, session, now=BASE)   # canon job COMPLETED, nothing finalized
    db.close()
    canon = env.q(lambda db: rows(db, select(CanonAnalysis))[0])
    assert canon.status == "queued"

    fresh = env.orch()
    tick = fresh.tick(env.wf_id, now=BASE)
    assert (env.project_id, "canon") in tick.finalized
    assert tick.current_steps == {env.project_id: "story"}
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).status == "completed"


def test_crash_between_begin_and_enqueue_is_repaired(env):
    o1 = env.orch()
    SnapshotSource().run(env.ctx, env.project_id)
    db = env.sf()
    project = db.get(StoryProject, env.project_id)
    begun = CanonStep().begin(db, env.ctx, project)      # domain row created, process "crashes"
    db.close()
    assert begun is not None
    assert env.jobs() == [] and env.count(CanonAnalysis) == 1

    o2 = env.orch()
    res = o2.tick(env.wf_id, now=BASE)

    (job,) = env.jobs()
    assert job.dedupe_key == f"canon:{begun[0]}"
    assert env.count(CanonAnalysis) == 1, "repair must reuse the domain row"
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).pipeline_job_id == job.id
    assert res.jobs_enqueued == [job.id]
    del o1


def test_crash_between_enqueue_and_link_reuses_job_even_if_already_completed(env):
    SnapshotSource().run(env.ctx, env.project_id)
    db = env.sf()
    project = db.get(StoryProject, env.project_id)
    domain_id, spec = CanonStep().begin(db, env.ctx, project)
    queue.enqueue_job(db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key, role=spec.role,
                      session_id=env.session_id, now=BASE)   # enqueued, never linked
    session = db.get(WorkflowSession, env.session_id)
    env.dispatcher.run_round(db, session, now=BASE)          # ...and even completed (frees dedupe slot)
    db.close()
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).pipeline_job_id is None

    res = env.orch().tick(env.wf_id, now=BASE)

    assert len(env.jobs("canon_analysis")) == 1, "must link the finished job, not enqueue a duplicate"
    assert (env.project_id, "canon") in res.finalized
    assert res.current_steps == {env.project_id: "story"}


# --------------------------------------------------------------------- terminal workflows


def test_finished_workflow_enqueues_nothing(env):
    orch = env.orch()
    orch.run_until_idle(env.wf_id, now=BASE)
    jobs_before = [j.id for j in env.jobs()]
    finished_at = env.wf().finished_at

    for _ in range(3):
        res = orch.tick(env.wf_id, now=BASE + timedelta(hours=1))
        assert res.jobs_enqueued == [] and res.workflow_status == "finished"
    rnd = orch.run_round(env.wf_id, now=BASE + timedelta(hours=1))
    assert rnd.outcome is None and not rnd.progressed
    assert [j.id for j in env.jobs()] == jobs_before
    assert env.wf().finished_at == finished_at, "finished_at is set only once"


@pytest.mark.parametrize("status", ["paused", "abandoned"])
def test_paused_or_abandoned_workflow_enqueues_nothing(env, status):
    db = env.sf()
    db.execute(update(ChannelWorkflow).where(ChannelWorkflow.id == env.wf_id).values(status=status))
    db.commit()
    db.close()
    orch = env.orch()
    res = orch.tick(env.wf_id, now=BASE)
    out = orch.run_until_idle(env.wf_id, now=BASE)

    assert res.jobs_enqueued == [] and env.jobs() == []
    assert env.count(SourceSnapshot) == 0 and env.count(CanonAnalysis) == 0
    assert out.status in ("paused", "blocked") and env.wf_status() == status


# --------------------------------------------------------------------- failure / retry


def _fail_canon_env(session_factory):
    e = Env(session_factory, runners=())
    e.add_runner("fake", results=[ResultCode.TASK_FAILED] * 5)   # settings.max_attempts business failures
    return e


def test_failed_step_does_not_advance_and_pauses(session_factory):
    env = _fail_canon_env(session_factory)
    orch = env.orch()
    out = orch.run_until_idle(env.wf_id, now=BASE)

    assert out.status == "paused" and env.wf_status() == "paused"
    (job,) = env.jobs()
    assert job.status == "failed" and job.attempts == 5
    canon = env.q(lambda db: rows(db, select(CanonAnalysis))[0])
    assert canon.status == "failed" and canon.error_code == "task_failed"
    assert env.count(StoryGeneration) == 0

    before = len(env.jobs())
    for _ in range(3):
        orch.tick(env.wf_id, now=BASE)
    assert len(env.jobs()) == before, "a failed step never retries automatically"
    assert env.wf_status() == "paused"


def test_infra_exhausted_job_pauses_with_error_code_preserved(session_factory):
    env = Env(session_factory, runners=())
    env.add_runner("fake", results=[CRASH] * 5)
    out = env.orch().run_until_idle(env.wf_id, now=BASE)

    assert out.status == "paused"
    (job,) = env.jobs()
    assert job.status == "failed" and job.last_error_code == "INFRA_EXHAUSTED"
    assert job.attempts == 0 and job.infrastructure_failures == 5
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).error_code == "INFRA_EXHAUSTED"


def test_all_agents_unavailable_require_attention_pauses(session_factory):
    env = Env(session_factory, policy="require_attention", runners=())
    out = env.orch().run_until_idle(env.wf_id, now=BASE)

    assert out.status == "paused"
    (job,) = env.jobs()
    assert job.status == "failed" and job.last_error_code == "ALL_AGENTS_UNAVAILABLE"
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).error_code == "ALL_AGENTS_UNAVAILABLE"


def test_retry_failed_step_creates_fresh_row_and_resumes(session_factory):
    env = _fail_canon_env(session_factory)
    orch = env.orch()
    orch.run_until_idle(env.wf_id, now=BASE)
    assert env.wf_status() == "paused"
    old_canon = env.q(lambda db: rows(db, select(CanonAnalysis))[0])

    step = orch.retry_failed_step(env.project_id, now=BASE)

    assert step == "canon"
    canons = env.q(lambda db: rows(db, select(CanonAnalysis).order_by(CanonAnalysis.created_at)))
    assert len(canons) == 2
    fresh = [c for c in canons if c.id != old_canon.id][0]
    assert fresh.status == "queued" and fresh.pipeline_job_id is not None
    assert [c for c in canons if c.id == old_canon.id][0].status == "failed", "history is kept"
    assert env.wf_status() == "active"
    assert len(env.active_jobs()) == 1

    out = orch.run_until_idle(env.wf_id, now=BASE)   # FakeRunner queue is now empty -> success
    assert out.status == "completed"


def test_resume_reactivates_and_rearms_failed_step(session_factory):
    env = _fail_canon_env(session_factory)
    orch = env.orch()
    orch.run_until_idle(env.wf_id, now=BASE)

    assert env.orch().resume(env.wf_id, now=BASE) == "active"   # a fresh process may do it
    assert env.count(CanonAnalysis) == 2 and len(env.active_jobs()) == 1
    assert env.orch().run_until_idle(env.wf_id, now=BASE).status == "completed"
    assert env.orch().resume(env.wf_id) == "finished", "resume never reopens a finished workflow"


def test_inline_source_failure_pauses_and_resume_retries(env):
    bad = SnapshotSource(fail=True)
    orch = env.orch(bad)
    out = orch.run_until_idle(env.wf_id, now=BASE)
    assert out.status == "paused" and env.jobs() == [] and bad.runs == 1
    orch.tick(env.wf_id, now=BASE)
    assert bad.runs == 1, "paused workflow does not retry the source"

    good = env.orch(SnapshotSource())
    assert good.resume(env.wf_id, now=BASE) == "active"
    assert good.run_until_idle(env.wf_id, now=BASE).status == "completed"


# --------------------------------------------------------------------- Phase 2 semantics preserved


def test_waiting_capacity_is_not_a_failure_and_run_until_idle_does_not_spin(session_factory):
    env = Env(session_factory, runners=())
    orch = env.orch()

    out = orch.run_until_idle(env.wf_id, now=BASE)
    assert out.status == "blocked" and out.blocked_reason == "waiting_capacity"
    assert out.rounds <= 2
    assert env.wf_status() == "active", "waiting_capacity must not fail or pause the workflow"
    (job,) = env.jobs()
    assert job.status == "waiting_capacity" and job.attempts == 0 and job.infrastructure_failures == 0
    assert env.q(lambda db: rows(db, select(CanonAnalysis))[0]).status == "queued"

    again = orch.run_until_idle(env.wf_id, now=BASE)   # still nothing to run: must return, not loop
    assert again.status == "blocked" and again.rounds <= 2
    assert len(env.jobs()) == 1

    fake = env.add_runner("late")                       # capacity appears
    done = orch.run_until_idle(env.wf_id, now=BASE)
    assert done.status == "completed"
    assert len(env.jobs("canon_analysis")) == 1, "the parked job was resumed, not duplicated"
    assert [p.task_config["step"] for p in fake.invocations] == ["canon", "story", "tts", "audio"]


def test_queued_job_scheduled_in_future_blocks_without_spinning(session_factory):
    env = Env(session_factory, runners=())
    env.add_runner("fake", results=[RunnerResult(code=ResultCode.RATE_LIMITED, retry_after=600)])
    orch = env.orch()

    out = orch.run_until_idle(env.wf_id, now=BASE)
    assert out.status == "blocked" and out.blocked_reason == "no_progress"
    assert out.rounds <= 3
    assert env.wf_status() == "active"
    (job,) = env.jobs()
    assert job.status == "queued" and job.scheduled_at == BASE + timedelta(seconds=600) and job.attempts == 0

    later = orch.run_until_idle(env.wf_id, now=BASE + timedelta(seconds=601))
    assert later.status == "completed"


def test_infra_crash_requeues_without_business_attempt_and_workflow_continues(session_factory):
    env = Env(session_factory, runners=())
    env.add_runner("fake", results=[CRASH])
    out = env.orch().run_until_idle(env.wf_id, now=BASE)

    assert out.status == "completed"
    canon_job = env.jobs("canon_analysis")[0]
    assert canon_job.attempts == 0, "infra failures must not consume business attempts"
    assert canon_job.infrastructure_failures == 1 and canon_job.execution_count == 2
    assert len(env.jobs("canon_analysis")) == 1, "requeue reuses the same job; orchestrator adds none"


def test_quota_exhausted_fails_over_to_second_runner(session_factory):
    env = Env(session_factory, prefs={"story_writer": ["a"], "tts_adapter": ["a"]}, runners=())
    a = env.add_runner("a", results=[ResultCode.QUOTA_EXHAUSTED])
    b = env.add_runner("b")
    out = env.orch().run_until_idle(env.wf_id, now=BASE)

    assert out.status == "completed"
    assert len(a.invocations) == 1, "quota-exhausted runner is parked after one attempt"
    assert [p.task_config["step"] for p in b.invocations] == ["canon", "story", "tts", "audio"]
    assert env.jobs("canon_analysis")[0].attempts == 0
    assert env.count(CanonAnalysis) == 1


def test_runner_of_another_session_never_receives_the_job(session_factory):
    env = Env(session_factory)
    db = session_factory()
    other = WorkflowSession(mode="auto", status="active")
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()
    outsider = env.add_runner("outsider", session_id=other_id)

    out = env.orch().run_until_idle(env.wf_id, now=BASE)

    assert out.status == "completed"
    assert outsider.invocations == []
    assert len(env.fakes["fake"].invocations) == 4
    assert {j.workflow_session_id for j in env.jobs()} == {env.session_id}


def test_run_until_idle_is_bounded_by_max_rounds(env):
    out = env.orch().run_until_idle(env.wf_id, max_rounds=2, now=BASE)

    assert out.status == "max_rounds" and out.rounds == 2
    assert env.wf_status() == "active"
    assert len(env.fakes["fake"].invocations) == 2
    assert env.orch().run_until_idle(env.wf_id, now=BASE).status == "completed"


def test_orchestrator_only_reaches_runners_through_the_dispatcher(env):
    """tick / resume / retry never execute; only run_round does (one dispatch per round)."""
    orch = env.orch()
    orch.tick(env.wf_id, now=BASE)
    orch.resume(env.wf_id, now=BASE)
    assert env.fakes["fake"].invocations == []
    orch.run_round(env.wf_id, now=BASE)
    assert len(env.fakes["fake"].invocations) == 1
