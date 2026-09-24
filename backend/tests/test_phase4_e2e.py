"""Phase 4 end-to-end: source -> canon -> story -> TTS -> audio, all through the Phase 2
Dispatcher with deterministic fakes; plus resume/failure scenarios A-E.

No sleeps, injected clock, real SQLite file, real ArtifactStore under tmp_path.
"""

from datetime import datetime, timedelta
from pathlib import PurePosixPath

import pytest
from sqlalchemy import func, select

from storyflow.agents import CRASH, TIMEOUT, AgentRunner, FakeRunner, RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher
from storyflow.models import (
    AudioChunk, AudioGeneration, CanonAnalysis, ChannelWorkflow, ChannelWorkflowStatus, JobStatus,
    PipelineJob, RunnerAttempt, RunnerInstance, SourceSnapshot, StoryGeneration, StoryProject,
    StoryVersion, TTSGeneration, WorkflowSession,
)
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import OutputValidatingRunner, PipelineContext
from storyflow.protocol import ResultCode
from storyflow.roles import Role
from storyflow.story_steps import STORY_VALIDATORS, CanonStep, FakeStoryPipelineRunner, SourceStep, StoryStep
from storyflow.subtitles import FakeSubtitleClient
from storyflow.tts_steps import TTS_VALIDATORS, AudioStep, FakeAudioRunner, FakeTTSAdapterRunner, TTSStep

NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "The hero wakes.", "start": 0.0}, {"text": "The rival waits.", "start": 2.0}]}
ROLES = [Role.STORY_WRITER.value, Role.TTS_ADAPTER.value]
CHUNKING = {"preferred_chunk_chars_max": 60, "avoid_chunk_below_chars": 10}
STEP_KINDS = ["canon_analysis", "story_generation", "tts_generation", "audio_generation"]


class Router(AgentRunner):
    """One physical runner serving several roles: routes by task_config.step to the fakes.
    Test-only glue; the Dispatcher still owns selection and result handling."""

    def __init__(self, store, *, story=None, tts=None, audio=None, runner_type="router"):
        self.runner_type = runner_type
        self.story = story or FakeStoryPipelineRunner(store)
        self.tts = tts or FakeTTSAdapterRunner(store, chunking=CHUNKING)
        self.audio = audio or FakeAudioRunner(store)
        self.steps_seen = []
        validators = {**STORY_VALIDATORS, **TTS_VALIDATORS}
        self._delegates = {
            "canon": OutputValidatingRunner(self.story, store, validators),
            "story": OutputValidatingRunner(self.story, store, validators),
            "tts": OutputValidatingRunner(self.tts, store, validators),
            "audio": OutputValidatingRunner(self.audio, store, validators),
        }

    def execute(self, packet):
        step = packet.task_config["step"]
        self.steps_seen.append(step)
        return self._delegates[step].execute(packet)

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED


class Env:
    def __init__(self, session_factory, tmp_path):
        self.sf = session_factory
        self.store = ArtifactStore(tmp_path / "artifacts")
        self.clock = [NOW]
        self.ctx = PipelineContext(session_factory=session_factory, store=self.store,
                                   subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}),
                                   clock=lambda: self.clock[0])
        db = session_factory()
        self.session_id = self._session(db)
        self.other_session_id = self._session(db)
        wf = ChannelWorkflow(workflow_session_id=self.session_id, name="chan", mode="auto", config={
            "source": {"video_id": "vid", "languages": ["en"]},
            "story": {"branch": "a darker turn"},
            "tts": {"voice": "narrator"}})
        db.add(wf)
        db.commit()
        proj = StoryProject(channel_workflow_id=wf.id, title="Tale", slug="tale")
        db.add(proj)
        db.commit()
        self.workflow_id, self.project_id = wf.id, proj.id
        db.close()
        self.registry = RunnerRegistry()
        self.routers = {}
        # A runner in ANOTHER session: must never receive session A's jobs.
        self.foreign = FakeRunner("foreign", roles=ROLES)
        self._register(self.other_session_id, self.foreign, "foreign")

    def _session(self, db):
        s = WorkflowSession(mode="auto", status="active", role_preferences={})
        db.add(s)
        db.commit()
        return s.id

    def _register(self, session_id, agent, name):
        db = self.sf()
        inst = RunnerInstance(workflow_session_id=session_id, runner_type=name, max_concurrency=1,
                              supported_roles=ROLES)
        db.add(inst)
        db.commit()
        self.registry.register(inst.id, agent)
        iid = inst.id
        db.close()
        return iid

    def add_router(self, name="router", **kw):
        r = Router(self.store, runner_type=name, **kw)
        self.routers[name] = r
        return self._register(self.session_id, r, name), r

    def orchestrator(self):
        """A brand-new orchestrator + dispatcher: no carried in-memory state."""
        return Orchestrator(self.ctx, Dispatcher(self.registry), SourceStep(),
                            [CanonStep(), StoryStep(), TTSStep(), AudioStep()])

    def q(self, fn):
        db = self.sf()
        try:
            return fn(db)
        finally:
            db.close()

    def count(self, model, *where):
        return self.q(lambda db: db.scalar(select(func.count()).select_from(model).where(*where)))

    def jobs(self):
        return self.q(lambda db: db.scalars(select(PipelineJob).order_by(PipelineJob.created_at),
                                            execution_options={"populate_existing": True}).all())

    def wf_status(self):
        return self.q(lambda db: db.get(ChannelWorkflow, self.workflow_id, populate_existing=True).status)


@pytest.fixture
def env(session_factory, tmp_path):
    return Env(session_factory, tmp_path)


def assert_relative(path):
    assert path and not PurePosixPath(path.replace("\\", "/")).is_absolute() and ":" not in path
    assert ".." not in PurePosixPath(path.replace("\\", "/")).parts


def persisted_paths(env):
    def read(db):
        paths = [v.content_path for v in db.scalars(select(StoryVersion))]
        paths += [c.artifact_path for c in db.scalars(select(AudioChunk))]
        paths += [a.store_dir for a in db.scalars(select(AudioGeneration))]
        paths += [s.meta.get("artifact_path") for s in db.scalars(select(SourceSnapshot))]
        return paths
    return env.q(read)


def chain_rows(env):
    def read(db):
        return dict(
            snapshots=db.scalars(select(SourceSnapshot)).all(),
            canons=db.scalars(select(CanonAnalysis)).all(),
            gens=db.scalars(select(StoryGeneration)).all(),
            versions=db.scalars(select(StoryVersion)).all(),
            ttss=db.scalars(select(TTSGeneration)).all(),
            audios=db.scalars(select(AudioGeneration)).all(),
            chunks=db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index)).all(),
        )
    return env.q(read)


# --- full pipeline ------------------------------------------------------------


def test_end_to_end_source_to_audio_chunks(env):
    _, router = env.add_router()
    result = env.orchestrator().run_until_idle(env.workflow_id)
    assert result.status == "completed"
    assert env.wf_status() == ChannelWorkflowStatus.FINISHED.value

    rows = chain_rows(env)
    (snap,), (canon,), (gen,), (version,), (tts,), (audio,) = (
        rows[k] for k in ("snapshots", "canons", "gens", "versions", "ttss", "audios"))
    # every domain record links to the previous one
    assert snap.story_project_id == env.project_id
    assert canon.source_snapshot_id == snap.id and canon.status == "completed" and canon.canon
    assert gen.source_snapshot_id == snap.id and gen.canon_analysis_id == canon.id
    assert gen.status == "completed"
    assert version.story_generation_id == gen.id and version.story_project_id == env.project_id
    assert tts.story_version_id == version.id and tts.status == "completed"
    assert audio.tts_generation_id == tts.id and audio.status == "completed" and audio.run_number == 1
    chunks = rows["chunks"]
    assert len(chunks) >= 2 and audio.chunk_count == len(chunks)
    assert [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c.audio_generation_id == audio.id for c in chunks)

    # jobs ran through the dispatcher: one completed job per step, closed attempts, no failures
    jobs = env.jobs()
    assert sorted(j.kind for j in jobs) == sorted(STEP_KINDS)
    assert all(j.status == JobStatus.COMPLETED.value and j.attempts == 0 and j.infrastructure_failures == 0
               for j in jobs)
    assert all(j.workflow_session_id == env.session_id for j in jobs)
    attempts = env.q(lambda db: db.scalars(select(RunnerAttempt)).all())
    assert len(attempts) == 4 and all(a.result_type == "success" for a in attempts)
    assert router.steps_seen == ["canon", "story", "tts", "audio"]
    assert env.q(lambda db: [r.active_count for r in db.scalars(select(RunnerInstance))]) == [0, 0]

    # artifacts exist and resolve; DB holds only relative paths
    for p in persisted_paths(env):
        assert_relative(p)
    assert env.store.exists(version.content_path)
    assert all(env.store.exists(c.artifact_path) for c in chunks)
    assert env.store.read(version.content_path).decode("utf-8").strip() == version.content.strip()

    # session isolation: foreign-session runner never touched anything
    assert env.foreign.invocations == []


def test_rerun_after_completion_creates_no_work(env):
    env.add_router()
    orch = env.orchestrator()
    assert orch.run_until_idle(env.workflow_id).status == "completed"
    before = (len(env.jobs()), {k: len(v) for k, v in chain_rows(env).items()})
    for _ in range(3):
        env.orchestrator().tick(env.workflow_id)
        env.orchestrator().run_round(env.workflow_id)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    after = (len(env.jobs()), {k: len(v) for k, v in chain_rows(env).items()})
    assert before == after


# --- restart / resume (scenario E) --------------------------------------------


def test_restart_between_every_step_reconstructs_from_db(env):
    """A brand-new Orchestrator + Dispatcher (fresh in-memory state) takes over after each
    single round; the pipeline still completes with exactly one of everything."""
    env.add_router()
    for _ in range(60):
        orch = env.orchestrator()
        res = orch.run_round(env.workflow_id)
        if env.wf_status() == ChannelWorkflowStatus.FINISHED.value:
            break
    assert env.wf_status() == ChannelWorkflowStatus.FINISHED.value
    rows = chain_rows(env)
    assert {k: len(v) for k, v in rows.items() if k != "chunks"} == {
        "snapshots": 1, "canons": 1, "gens": 1, "versions": 1, "ttss": 1, "audios": 1}
    assert sorted(j.kind for j in env.jobs()) == sorted(STEP_KINDS)


def test_restart_mid_pipeline_continues_from_exact_step(env):
    env.add_router()
    orch = env.orchestrator()
    while True:
        orch.run_round(env.workflow_id)
        if chain_rows(env)["versions"]:
            break
    # process "dies" here: story is done, TTS scheduled (queued) but not yet executed
    rows = chain_rows(env)
    assert [t.status for t in rows["ttss"]] == ["queued"] and rows["audios"] == []
    assert [j.status for j in env.jobs() if j.kind == "tts_generation"] == ["queued"]
    assert env.wf_status() == ChannelWorkflowStatus.ACTIVE.value
    fresh = env.orchestrator()
    tick = fresh.tick(env.workflow_id)
    assert tick.current_steps[env.project_id] == "tts"
    assert fresh.run_until_idle(env.workflow_id).status == "completed"
    assert len(chain_rows(env)["versions"]) == 1 and len(chain_rows(env)["ttss"]) == 1


# --- scenario A: quota -------------------------------------------------------


def test_scenario_a_quota_exhausted_fails_over_without_business_failure(env):
    quota = FakeStoryPipelineRunner(env.store, results=[ResultCode.QUOTA_EXHAUSTED])
    iid1, r1 = env.add_router("first", story=quota)
    iid2, r2 = env.add_router("second")
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    canon_job = next(j for j in env.jobs() if j.kind == "canon_analysis")
    assert canon_job.attempts == 0 and canon_job.infrastructure_failures == 0
    assert env.count(StoryVersion) == 1 and env.count(AudioGeneration) == 1
    states = env.q(lambda db: {r.runner_type: r.state for r in db.scalars(select(RunnerInstance))})
    assert states["first"] == "quota_exhausted" and states["second"] in ("ready", "busy")
    assert quota.invocations  # the quota-hit runner was actually tried first
    assert "canon" in r2.steps_seen
    assert env.foreign.invocations == []


# --- scenario B: infra crash/timeout -----------------------------------------


@pytest.mark.parametrize("sentinel", [CRASH, TIMEOUT])
def test_scenario_b_infra_failure_resumes_without_duplicates(env, sentinel):
    story = FakeStoryPipelineRunner(env.store, results=[ResultCode.SUCCESS, sentinel])  # canon ok, story crash
    env.add_router(story=story)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    story_job = next(j for j in env.jobs() if j.kind == "story_generation")
    assert story_job.infrastructure_failures == 1 and story_job.attempts == 0
    assert story_job.execution_count == 2
    rows = chain_rows(env)
    assert len(rows["gens"]) == 1 and len(rows["versions"]) == 1 and len(rows["ttss"]) == 1
    assert len(rows["audios"]) == 1


def test_scenario_b_stale_lease_is_recovered_on_restart(env):
    """A worker died mid-job (lease expired): a new orchestrator recovers it as an
    infrastructure failure and the pipeline finishes without duplicates."""
    env.add_router()
    orch = env.orchestrator()
    orch.tick(env.workflow_id)  # source done, canon job enqueued
    job = env.jobs()[0]
    assert job.kind == "canon_analysis"
    from storyflow import queue
    db = env.sf()
    claimed = queue.claim_next_job(db, "dead-worker", 5, now=NOW)
    db.close()
    assert claimed.id == job.id
    env.clock[0] = NOW + timedelta(minutes=10)  # lease long expired
    fresh = env.orchestrator()
    assert fresh.tick(env.workflow_id).recovered == 1
    assert fresh.run_until_idle(env.workflow_id).status == "completed"
    canon_job = next(j for j in env.jobs() if j.kind == "canon_analysis")
    assert canon_job.infrastructure_failures == 1 and canon_job.attempts == 0
    assert env.count(CanonAnalysis) == 1 and env.count(StoryVersion) == 1


# --- scenario C: invalid story output ----------------------------------------


def test_scenario_c_invalid_story_output_is_bounded_business_failure(env):
    story = FakeStoryPipelineRunner(env.store, emit_invalid={"story"})
    env.add_router(story=story)
    result = env.orchestrator().run_until_idle(env.workflow_id)
    assert result.status == "paused"
    assert env.wf_status() == ChannelWorkflowStatus.PAUSED.value
    story_job = next(j for j in env.jobs() if j.kind == "story_generation")
    assert story_job.status == JobStatus.FAILED.value
    assert story_job.attempts == story_job.max_attempts  # bounded retry, business counter only
    assert story_job.infrastructure_failures == 0
    assert env.count(StoryVersion) == 0
    assert env.count(TTSGeneration) == 0
    gen = chain_rows(env)["gens"]
    assert len(gen) == 1 and gen[0].status == "failed" and gen[0].error_code
    assert not any(j.kind == "tts_generation" for j in env.jobs())


def test_scenario_c_operator_retry_creates_fresh_generation_and_finishes(env):
    story = FakeStoryPipelineRunner(env.store, emit_invalid={"story"})
    env.add_router(story=story)
    orch = env.orchestrator()
    assert orch.run_until_idle(env.workflow_id).status == "paused"
    story.emit_invalid = False  # provider recovered
    assert orch.resume(env.workflow_id)
    assert orch.run_until_idle(env.workflow_id).status == "completed"
    rows = chain_rows(env)
    assert sorted(g.status for g in rows["gens"]) == ["completed", "failed"]
    assert len(rows["versions"]) == 1 and rows["versions"][0].version_number == 1


# --- scenario D: TTS partial failure -----------------------------------------


def test_scenario_d_tts_partial_failure_preserves_completed_chunks(env):
    audio = FakeAudioRunner(env.store, fail_after=2)
    env.add_router(audio=audio)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    audio_job = next(j for j in env.jobs() if j.kind == "audio_generation")
    assert audio_job.attempts == 1 and audio_job.infrastructure_failures == 0
    rows = chain_rows(env)
    assert len(rows["audios"]) == 1 and rows["audios"][0].run_number == 1
    chunks = rows["chunks"]
    assert len(chunks) > 2
    assert [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    # first two files were produced exactly once (never rewritten); the retry only made the rest
    assert len(audio.produced) == len(chunks) and len(set(audio.produced)) == len(chunks)
    assert len(audio.skipped) == 2
    assert not list(env.store.root.rglob(".tmp-*"))


def test_scenario_d_invalid_audio_is_business_failure_then_retry_completes(env):
    audio = FakeAudioRunner(env.store, invalid_output=True)
    env.add_router(audio=audio)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    audio_job = next(j for j in env.jobs() if j.kind == "audio_generation")
    assert audio_job.attempts == 1
    assert env.count(AudioGeneration) == 1
    chunks = chain_rows(env)["chunks"]
    assert len({c.chunk_index for c in chunks}) == len(chunks)


# --- capacity ------------------------------------------------------------------


def test_waiting_capacity_is_not_a_failure_and_resumes(env):
    """No runner in the session: job parks (workflow stays ACTIVE, no failure counters);
    once a runner appears the same job runs and the pipeline completes."""
    orch = env.orchestrator()
    res = orch.run_until_idle(env.workflow_id)
    assert res.status == "blocked"
    assert env.wf_status() == ChannelWorkflowStatus.ACTIVE.value
    job = env.jobs()[0]
    assert job.status == JobStatus.WAITING_CAPACITY.value
    assert job.attempts == 0 and job.infrastructure_failures == 0 and job.outcome is None
    env.add_router()
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    assert len([j for j in env.jobs() if j.kind == "canon_analysis"]) == 1


def test_only_session_runner_receives_work_even_when_foreign_runner_is_idle(env):
    env.add_router()
    env.orchestrator().run_until_idle(env.workflow_id)
    assert env.foreign.invocations == []
    assert env.routers["router"].steps_seen == ["canon", "story", "tts", "audio"]
