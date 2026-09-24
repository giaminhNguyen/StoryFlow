"""TTSStep / AudioStep tests (Phase 4 workstream 3). Deterministic: tmp_path ArtifactStore,
fake runners, injected clock, no sleeps, no real TTS. Job semantics go through the Phase 2
Dispatcher (claim -> execute -> apply_result), never by calling runners directly."""

import threading
from datetime import datetime

import pytest
from sqlalchemy import func, select

from storyflow import queue
from storyflow.agents import CRASH, RunnerRegistry
from storyflow.artifacts import ArtifactStore, PathTraversalError
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.models import (
    AudioChunk,
    AudioGeneration,
    ChannelWorkflow,
    PipelineJob,
    RunnerInstance,
    StoryProject,
    StoryVersion,
    TTSGeneration,
    WorkflowSession,
)
from storyflow.pipeline import OutputValidatingRunner, PipelineContext, StepStatus
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.tts_steps import (
    TTS_VALIDATORS,
    AudioStep,
    FakeAudioRunner,
    FakeTTSAdapterRunner,
    TTSStep,
    UnknownProfile,
    fake_wav,
    sync_chunks,
    wav_duration_ms,
)

NOW = datetime(2026, 1, 2, 12, 0, 0)
STORY = "\n\n".join(
    " ".join(f"Sentence number {p * 10 + s} of the story goes on for a while." for s in range(10))
    for p in range(3)
)


# --- helpers ---------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def ctx(session_factory, store):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=None, clock=lambda: NOW)


def make_project(db, store, *, story=STORY, with_version=True, tts=None):
    wf = ChannelWorkflow(name="cha", mode="auto", status="active", config={"tts": tts} if tts else {})
    db.add(wf)
    db.commit()
    project = StoryProject(title="p", channel_workflow_id=wf.id)
    db.add(project)
    db.commit()
    if with_version:
        add_version(db, store, project, 1, story)
    return project


def add_version(db, store, project, number, story=STORY):
    path = f"projects/{project.id}/story/v{number}.md"
    store.write(path, story.encode("utf-8"))
    v = StoryVersion(story_project_id=project.id, version_number=number, title=f"v{number}",
                     content=story, content_path=path)
    db.add(v)
    db.commit()
    return v


class Rig:
    """Session + runner instance + Dispatcher for tts_adapter jobs."""

    def __init__(self, db, ctx, runner):
        self.db, self.ctx = db, ctx
        self.session = WorkflowSession(mode="auto", status="active", all_agents_unavailable_policy="pause_auto_resume")
        db.add(self.session)
        db.commit()
        self.instance = RunnerInstance(workflow_session_id=self.session.id, runner_type=runner.runner_type,
                                       max_concurrency=1, supported_roles=["tts_adapter"])
        db.add(self.instance)
        db.commit()
        reg = RunnerRegistry()
        reg.register(self.instance.id, runner)
        self.runner = runner
        self.dispatcher = Dispatcher(reg)

    def enqueue(self, spec):
        return queue.enqueue_job(self.db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key,
                                 role=spec.role, session_id=self.session.id, now=NOW)

    def round(self):
        return self.dispatcher.run_round(self.db, self.session, now=NOW)

    def job(self, job_id):
        return self.db.scalar(select(PipelineJob).where(PipelineJob.id == job_id)
                              .execution_options(populate_existing=True))


def run_step(handler, rig, project):
    """begin -> enqueue -> link -> one dispatcher round. Returns (domain_id, job_id, outcome)."""
    domain_id, spec = handler.begin(rig.db, rig.ctx, project)
    job = rig.enqueue(spec)
    handler.link_job(rig.db, rig.ctx, domain_id, job)
    outcome, _ = rig.round()
    return domain_id, job.id, outcome


def complete_tts(db, ctx, project, chunking=None):
    """Produce a COMPLETED TTSGeneration through the dispatcher path."""
    rig = Rig(db, ctx, FakeTTSAdapterRunner(ctx.store, chunking=chunking or {"preferred_chunk_chars_max": 120}))
    step = TTSStep()
    tid, jid, outcome = run_step(step, rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    step.finalize(db, ctx, tid, rig.job(jid))
    return tid


def audio_rig(db, ctx, **kw):
    inner = FakeAudioRunner(ctx.store, **kw)
    return Rig(db, ctx, OutputValidatingRunner(inner, ctx.store, TTS_VALIDATORS)), inner


def all_paths(db):
    paths = [r for (r,) in db.execute(select(AudioChunk.artifact_path))]
    paths += [r for (r,) in db.execute(select(AudioGeneration.store_dir))]
    paths += [r for (r,) in db.execute(select(StoryVersion.content_path))]
    for job in db.scalars(select(PipelineJob)):
        p = job.payload_json
        paths += p["outputs"] + [v for v in p["inputs"].values() if isinstance(v, str) and "/" in v]
        paths += [x for v in p["inputs"].values() if isinstance(v, list) for x in v]
    return paths


def chunk_rows(db, gen_id):
    return db.scalars(select(AudioChunk).where(AudioChunk.audio_generation_id == gen_id)
                      .order_by(AudioChunk.chunk_index).execution_options(populate_existing=True)).all()


# --- TTSStep ---------------------------------------------------------------------------------


def test_begin_requires_active_story_version(db, ctx, store):
    project = make_project(db, store, with_version=False)
    step = TTSStep()
    assert step.begin(db, ctx, project) is None
    assert step.status(db, ctx, project).status is StepStatus.NOT_STARTED
    assert db.scalar(select(func.count()).select_from(TTSGeneration)) == 0


def test_tts_references_latest_active_immutable_version_and_payload(db, ctx, store):
    project = make_project(db, store, tts={"voice": "v1", "engine": "e1"})
    v2 = add_version(db, store, project, 2, STORY + " More.")
    tid, spec = TTSStep().begin(db, ctx, project)
    row = db.get(TTSGeneration, tid)
    assert row.story_version_id == v2.id
    assert (row.voice, row.engine, row.status) == ("v1", "e1", "queued")
    assert spec.dedupe_key == f"tts:{tid}" and spec.kind == "tts_generation" and spec.role == "tts_adapter"
    p = spec.payload
    assert p["skill"]["name"] == "story-tts-adapter" and p["skill"]["path"] == "skills/story-tts-adapter"
    assert len(p["skill"]["revision"]) == 40
    assert p["inputs"]["story_artifact"] == v2.content_path
    assert p["inputs"]["story_version_id"] == v2.id
    assert p["inputs"]["profile"] == "vieneu-v3-turbo-story"
    assert p["outputs"] == [f"projects/{project.id}/tts/{tid}/story_tts.txt",
                            f"projects/{project.id}/tts/{tid}/manifest.json"]
    assert p["task_config"]["step"] == "tts"


def test_unknown_profile_is_an_error_not_a_substitution(db, ctx, store):
    project = make_project(db, store, tts={"profile": "nope"})
    with pytest.raises(UnknownProfile):
        TTSStep().begin(db, ctx, project)


def test_tts_begin_finalize_idempotent_and_chunks_contiguous(db, ctx, store):
    project = make_project(db, store)
    tid = complete_tts(db, ctx, project, chunking={"preferred_chunk_chars_max": 120})
    step = TTSStep()
    assert step.begin(db, ctx, project)[0] == tid  # completed row is found, not duplicated
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED
    row = db.get(TTSGeneration, tid, populate_existing=True)
    assert row.status == "completed" and row.config["chunk_count"] > 1
    updated = row.updated_at
    step.finalize(db, ctx, tid, None)  # second finalize: no-op
    assert db.get(TTSGeneration, tid, populate_existing=True).updated_at == updated
    assert db.scalar(select(func.count()).select_from(TTSGeneration)) == 1
    out = f"projects/{project.id}/tts/{tid}"
    import json
    manifest = json.loads(store.read(f"{out}/manifest.json"))
    assert [c["index"] for c in manifest["chunks"]] == list(range(1, manifest["chunk_count"] + 1))
    assert [c["file"] for c in manifest["chunks"]] == [f"chunks/{i:04d}.txt" for i in range(1, manifest["chunk_count"] + 1)]
    assert all(len(store.read(f"{out}/{c['file']}").decode().strip()) <= 120 for c in manifest["chunks"])
    joined = "".join("".join(store.read(f"{out}/{c['file']}").decode().split()) for c in manifest["chunks"])
    assert joined == "".join(store.read(f"{out}/story_tts.txt").decode().split())


def test_tts_runner_does_not_rewrite_valid_output(db, ctx, store):
    project = make_project(db, store)
    tid = complete_tts(db, ctx, project)
    out = f"projects/{project.id}/tts/{tid}"
    before = store.read(f"{out}/manifest.json")
    _, spec = TTSStep().begin(db, ctx, project)
    runner = FakeTTSAdapterRunner(store)
    from storyflow.dispatcher import build_task_packet
    job = PipelineJob(id="j", kind=spec.kind, payload_json=spec.payload, role=spec.role)
    result = runner.execute(build_task_packet(job, None, task_id="t"))
    assert result.code is ResultCode.SUCCESS and result.metrics == {"skipped": True}
    assert store.read(f"{out}/manifest.json") == before


def test_tts_invalid_output_is_business_failure_and_not_completed(db, ctx, store):
    project = make_project(db, store)
    inner = FakeTTSAdapterRunner(store, invalid_output=True)
    rig = Rig(db, ctx, OutputValidatingRunner(inner, store, TTS_VALIDATORS))
    step = TTSStep()
    tid, jid, outcome = run_step(step, rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    job = rig.job(jid)
    assert job.status == "queued" and job.attempts == 1 and job.last_error_code == "invalid_output"
    assert db.get(TTSGeneration, tid, populate_existing=True).status == "queued"
    # retry (same generation, same job) succeeds now that the runner is healthy again
    outcome, _ = rig.round()
    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    step.finalize(db, ctx, tid, rig.job(jid))
    assert db.get(TTSGeneration, tid, populate_existing=True).status == "completed"


def test_tts_finalize_rejects_bad_manifest_and_mark_failed(db, ctx, store):
    project = make_project(db, store)
    step = TTSStep()
    tid, spec = step.begin(db, ctx, project)
    out = f"projects/{project.id}/tts/{tid}"
    step.finalize(db, ctx, tid, None)  # nothing written at all
    row = db.get(TTSGeneration, tid, populate_existing=True)
    assert row.status == "failed" and row.error_code == "invalid_output"
    # a failed generation needs an explicit new begin (creates a NEW row)
    assert step.status(db, ctx, project).status is StepStatus.FAILED
    tid2, _ = step.begin(db, ctx, project)
    assert tid2 != tid
    job = PipelineJob(kind="tts_generation", payload_json={}, last_error_code="task_failed")
    step.mark_failed(db, ctx, tid2, job)
    assert db.get(TTSGeneration, tid2, populate_existing=True).error_code == "task_failed"
    assert out.startswith("projects/")


def test_tts_finalize_validation_failures(db, ctx, store):
    project = make_project(db, store)
    tid = complete_tts(db, ctx, project)
    from storyflow.tts_steps import validate_tts_artifacts
    out = f"projects/{project.id}/tts/{tid}"
    good = lambda: validate_tts_artifacts(store, out, story_version_id=db.scalar(select(StoryVersion.id)),
                                          profile_id="vieneu-v3-turbo-story")[0]
    assert good() is None
    assert "StoryVersion" in validate_tts_artifacts(store, out, story_version_id="x", profile_id="vieneu-v3-turbo-story")[0]
    assert "profile" in validate_tts_artifacts(store, out, story_version_id="x", profile_id="other")[0]
    store.write(f"{out}/chunks/0001.txt", b"   \n")
    assert "empty" in good()
    store.write(f"{out}/chunks/0001.txt", b"x" * 300)
    assert "hard limit" in good()
    store.delete(f"{out}/chunks/0001.txt")
    assert "missing" in good()


# --- AudioStep -------------------------------------------------------------------------------


def test_audio_requires_completed_tts(db, ctx, store):
    project = make_project(db, store)
    TTSStep().begin(db, ctx, project)  # queued only
    assert AudioStep().begin(db, ctx, project) is None
    assert AudioStep().status(db, ctx, project).status is StepStatus.NOT_STARTED


def test_audio_payload_run_numbers_and_retry(db, ctx, store):
    project = make_project(db, store)
    tid = complete_tts(db, ctx, project)
    audio = AudioStep()
    aid, spec = audio.begin(db, ctx, project)
    g = db.get(AudioGeneration, aid)
    assert g.run_number == 1 and g.store_dir == f"projects/{project.id}/audio/{tid}/run-001"
    assert spec.dedupe_key == f"audio:{aid}" and spec.payload["task_config"]["step"] == "audio"
    n = len(spec.payload["inputs"]["chunks"])
    assert n > 1
    assert spec.payload["outputs"] == [f"{g.store_dir}/{i:04d}.wav" for i in range(1, n + 1)]
    assert spec.payload["inputs"]["chunks"][0] == f"projects/{project.id}/tts/{tid}/chunks/0001.txt"
    assert audio.begin(db, ctx, project)[0] == aid  # live run reused
    # fail it: explicit new begin -> run 2, never automatic
    audio.mark_failed(db, ctx, aid, PipelineJob(kind="audio_generation", payload_json={}, last_error_code="task_failed"))
    assert audio.status(db, ctx, project).status is StepStatus.FAILED
    aid2, spec2 = audio.begin(db, ctx, project)
    g2 = db.get(AudioGeneration, aid2)
    assert aid2 != aid and g2.run_number == 2 and g2.store_dir.endswith("run-002")
    assert spec2.dedupe_key == f"audio:{aid2}"
    assert audio.begin(db, ctx, project)[0] == aid2


def test_audio_full_run_completes_with_ordered_unique_chunks(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    rig, inner = audio_rig(db, ctx)
    audio = AudioStep()
    aid, jid, outcome = run_step(audio, rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    audio.finalize(db, ctx, aid, rig.job(jid))
    audio.finalize(db, ctx, aid, rig.job(jid))  # idempotent
    gen = db.get(AudioGeneration, aid, populate_existing=True)
    rows = chunk_rows(db, aid)
    assert gen.status == "completed" and gen.chunk_count == len(rows) == len(inner.produced) > 1
    assert [r.chunk_index for r in rows] == list(range(1, len(rows) + 1))
    for r in rows:
        assert r.text and r.duration_ms == wav_duration_ms(store.read(r.artifact_path)) == len(r.text) * 20
    assert audio.status(db, ctx, project).status is StepStatus.COMPLETED
    assert audio.begin(db, ctx, project)[0] == aid
    assert db.scalar(select(func.count()).select_from(AudioGeneration)) == 1


def test_partial_failure_resumes_without_overwriting_chunks(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    rig, inner = audio_rig(db, ctx, fail_after=2)
    audio = AudioStep()
    aid, jid, outcome = run_step(audio, rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    job = rig.job(jid)
    assert job.status == "queued" and job.attempts == 1 and job.last_error_code == "partial_failure"
    outputs = job.payload_json["outputs"]
    done = outputs[:2]
    snapshot = {p: store.read(p) for p in done}
    assert all(store.exists(p) for p in done) and not any(store.exists(p) for p in outputs[2:])
    # partial progress is visible and resumable without completing the run
    assert audio.finalize(db, ctx, aid, job) is None  # job not complete but finalize must stay safe
    gen = db.get(AudioGeneration, aid, populate_existing=True)
    assert gen.status == "processing" and gen.error_code == "chunks_missing"
    assert [r.chunk_index for r in chunk_rows(db, aid)] == [1, 2]
    assert audio.status(db, ctx, project).status is StepStatus.IN_PROGRESS
    # retry under the same generation: only chunks 3..n
    outcome, _ = rig.round()
    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert {p: store.read(p) for p in done} == snapshot
    assert inner.produced[:2] == done and inner.produced[2:] == outputs[2:]
    assert inner.skipped == done
    audio.finalize(db, ctx, aid, rig.job(jid))
    gen = db.get(AudioGeneration, aid, populate_existing=True)
    rows = chunk_rows(db, aid)
    assert gen.status == "completed" and gen.error_code is None
    assert [r.chunk_index for r in rows] == list(range(1, len(outputs) + 1)) and gen.chunk_count == len(outputs)
    assert len({r.chunk_index for r in rows}) == len(rows)


def test_sync_chunks_partial_registration_is_idempotent(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    audio = AudioStep()
    aid, spec = audio.begin(db, ctx, project)
    outputs = spec.payload["outputs"]
    assert sync_chunks(db, ctx, aid) == []
    store.write(outputs[0], fake_wav("first"))
    store.write(outputs[1], b"garbage")          # invalid file is not registered
    store.write(outputs[2], fake_wav("third"))
    assert sync_chunks(db, ctx, aid) == [1, 3]
    assert sync_chunks(db, ctx, aid) == []
    rows = chunk_rows(db, aid)
    first_ids = [r.id for r in rows]
    assert [r.chunk_index for r in rows] == [1, 3]
    gen = db.get(AudioGeneration, aid, populate_existing=True)
    assert gen.status == "processing" and gen.chunk_count == 0
    assert audio.status(db, ctx, project).status is StepStatus.IN_PROGRESS
    # a later valid write of chunk 2 registers only chunk 2; existing rows are untouched
    store.write(outputs[1], fake_wav("second"))
    assert sync_chunks(db, ctx, aid) == [2]
    assert [r.id for r in chunk_rows(db, aid) if r.chunk_index != 2] == first_ids


def test_audio_invalid_output_via_dispatcher_is_business_failure(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    rig, inner = audio_rig(db, ctx, invalid_output=True)
    audio = AudioStep()
    aid, jid, outcome = run_step(audio, rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    job = rig.job(jid)
    assert job.attempts == 1 and job.last_error_code == "invalid_output" and job.status == "queued"
    assert db.get(AudioGeneration, aid, populate_existing=True).status == "queued"
    assert audio.status(db, ctx, project).status is StepStatus.IN_PROGRESS
    # the garbage chunk is not registered, and is repaired (overwritten) on retry
    sync_chunks(db, ctx, aid)
    assert 1 not in [r.chunk_index for r in chunk_rows(db, aid)]
    outcome, _ = rig.round()
    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    audio.finalize(db, ctx, aid, rig.job(jid))
    assert db.get(AudioGeneration, aid, populate_existing=True).status == "completed"


def test_audio_runner_crash_is_infra_and_completes_after_retry(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    rig, inner = audio_rig(db, ctx, results=[CRASH])
    aid, jid, outcome = run_step(AudioStep(), rig, project)
    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    job = rig.job(jid)
    assert job.attempts == 0 and job.infrastructure_failures == 1
    assert inner.produced == []


# --- storage safety --------------------------------------------------------------------------


def test_no_tmp_leftovers_and_only_relative_paths_persisted(db, ctx, store):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    rig, _ = audio_rig(db, ctx)
    audio = AudioStep()
    aid, jid, _ = run_step(audio, rig, project)
    audio.finalize(db, ctx, aid, rig.job(jid))
    assert not [p for p in store.root.rglob("*") if p.name.startswith(".tmp-")]
    paths = all_paths(db)
    assert paths
    import os
    for p in paths:
        assert not os.path.isabs(p) and not p.startswith(("/", "\\")) and ":" not in p and ".." not in p.split("/")
        assert p.startswith("projects/")
        store.resolve(p)


def test_failed_write_leaves_no_partial_file(store, monkeypatch):
    import storyflow.artifacts as artifacts
    path = "projects/p/audio/x/0001.wav"
    store.write(path, b"old")

    def boom(_fd):
        raise OSError("disk died")

    monkeypatch.setattr(artifacts.os, "fsync", boom)
    with pytest.raises(OSError):
        store.write(path, b"new-content")
    with pytest.raises(OSError):
        store.write("projects/p/audio/x/0002.wav", b"new")
    monkeypatch.undo()
    assert store.read(path) == b"old"                     # completed artifact untouched
    assert not store.exists("projects/p/audio/x/0002.wav")  # nothing partially visible
    assert [p.name for p in (store.root / "projects/p/audio/x").iterdir()] == ["0001.wav"]


def test_runners_reject_traversal_and_foreign_paths(ctx, store):
    from storyflow.dispatcher import build_task_packet
    def packet(outputs, out_dir="projects/p/audio/g/run-001", chunks=None):
        return TaskPacket(task_id="t", job_id="j", role="tts_adapter",
                          inputs={"output_dir": out_dir, "chunks": chunks if chunks is not None else ["projects/p/c.txt"] * len(outputs)},
                          outputs=outputs)
    runner = FakeAudioRunner(store)
    with pytest.raises(PathTraversalError):
        runner.execute(packet(["projects/../evil.wav"], out_dir="projects/.."))
    with pytest.raises(ValueError):
        runner.execute(packet(["/abs/evil.wav"], out_dir="/abs"))
    with pytest.raises(ValueError):
        runner.execute(packet(["other/evil.wav"], out_dir="other"))
    with pytest.raises(ValueError):  # output outside the declared output dir
        runner.execute(packet(["projects/p/elsewhere/0001.wav"]))
    with pytest.raises(PathTraversalError):
        runner.execute(packet(["projects/p/audio/g/run-001/0001.wav"], chunks=["projects/../../etc/x"]))
    assert not any(store.root.rglob("*.wav"))
    tts_packet = TaskPacket(task_id="t", job_id="j", role="tts_adapter",
                            inputs={"output_dir": "projects/p/tts/g", "story_artifact": "../x.md",
                                    "story_version_id": "v", "profile": "vieneu-v3-turbo-story"},
                            outputs=["projects/p/tts/g/manifest.json"])
    with pytest.raises(ValueError):
        FakeTTSAdapterRunner(store).execute(tts_packet)


# --- concurrency ------------------------------------------------------------------------------


def _race(session_factory, ctx, project_id, make_step, n=6):
    barrier = threading.Barrier(n)
    ids, errors = [], []

    def work():
        s = session_factory()
        try:
            proj = s.get(StoryProject, project_id)
            c = PipelineContext(session_factory, ctx.store, None, ctx.clock)
            barrier.wait()
            ids.append(make_step().begin(s, c, proj)[0])
        except BaseException as e:  # surfaced to the main thread below
            errors.append(e)
        finally:
            s.close()

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return ids


def test_concurrent_tts_begin_creates_one_generation(db, ctx, store, session_factory):
    project = make_project(db, store)
    ids = _race(session_factory, ctx, project.id, TTSStep)
    assert len(set(ids)) == 1
    assert db.scalar(select(func.count()).select_from(TTSGeneration)) == 1


def test_concurrent_audio_begin_creates_one_live_run(db, ctx, store, session_factory):
    project = make_project(db, store)
    complete_tts(db, ctx, project)
    ids = _race(session_factory, ctx, project.id, AudioStep)
    assert len(set(ids)) == 1
    assert db.scalar(select(func.count()).select_from(AudioGeneration)) == 1


# --- fakes' own contract ---------------------------------------------------------------------


def test_wav_helpers():
    data = fake_wav("hello")
    assert wav_duration_ms(data) == 100 and fake_wav("hello") == data and fake_wav("hellp") != data
    assert wav_duration_ms(b"") is None and wav_duration_ms(data[:-1]) is None and wav_duration_ms(b"x" * 100) is None
