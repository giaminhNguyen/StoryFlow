"""Story domain tests (Phase 3): full chain, versioning/dedupe partial unique
indexes, one-active-per-snapshot canon analysis, artifact-backed content."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from storyflow.artifacts import ArtifactStore
from storyflow.models import (
    AudioChunk,
    AudioGeneration,
    CanonAnalysis,
    ChannelWorkflow,
    DomainStatus,
    SourceSnapshot,
    StoryGeneration,
    StoryProject,
    StoryVersion,
    TTSGeneration,
    VersionStatus,
    WorkflowSession,
)


def make_workflow(db, **kw):
    defaults = dict(name="cha", mode="auto", status="active")
    defaults.update(kw)
    wf = ChannelWorkflow(**defaults)
    db.add(wf)
    db.commit()
    return db.get(ChannelWorkflow, wf.id)


def make_project(db, workflow=None, **kw):
    defaults = dict(title="foo", slug=None)
    defaults.update(kw)
    p = StoryProject(**(defaults if workflow is None else dict(defaults, channel_workflow_id=workflow.id)))
    db.add(p)
    db.commit()
    return db.get(StoryProject, p.id)


def make_snapshot(db, project, *, number=1, **kw):
    defaults = dict(snapshot_number=number, title="snap", content="source text", status="active")
    defaults.update(kw)
    snap = SourceSnapshot(story_project_id=project.id, **defaults)
    db.add(snap)
    db.commit()
    return snap


def make_version(db, project, generation=None, *, number=1, status="active"):
    v = StoryVersion(
        story_generation_id=generation.id if generation else None,
        story_project_id=project.id,
        version_number=number,
        title=f"v{number}",
        content=f"chapter {number}",
        status=status,
    )
    db.add(v)
    db.commit()
    return v


# --- full chain -------------------------------------------------------------


def test_full_pipeline_chain_links(db):
    session = WorkflowSession(mode="auto", status="active", all_agents_unavailable_policy="pause_auto_resume")
    db.add(session)
    db.commit()
    wf = make_workflow(db, workflow_session_id=session.id)
    project = make_project(db, wf)
    snap = make_snapshot(db, project, content_hash="deadbeef", meta={"lang": "vi"}, number=1)
    analysis = CanonAnalysis(source_snapshot_id=snap.id, status="completed",
                             canon={"characters": ["A"], "plot": ["x"]})
    db.add(analysis)
    db.commit()
    gen = StoryGeneration(story_project_id=project.id, source_snapshot_id=snap.id,
                          canon_analysis_id=analysis.id, trigger="scheduled", status="completed")
    db.add(gen)
    db.commit()
    ver = make_version(db, project, gen, number=1)
    tts = TTSGeneration(story_version_id=ver.id, voice="mien", engine="vieneu", status="processing")
    db.add(tts)
    db.commit()
    audio = AudioGeneration(tts_generation_id=tts.id, run_number=1, status="processing", store_dir="proj/aud/1")
    db.add(audio)
    db.commit()
    chunk = AudioChunk(audio_generation_id=audio.id, chunk_index=0, artifact_path="proj/aud/1/0.wav",
                       duration_ms=5000, text="hello")
    db.add(chunk)
    db.commit()

    assert db.get(AudioChunk, chunk.id).audio_generation_id == audio.id
    assert db.get(TTSGeneration, tts.id).story_version_id == ver.id
    assert db.get(StoryVersion, ver.id).story_generation_id == gen.id
    assert db.get(CanonAnalysis, analysis.id).source_snapshot_id == snap.id
    assert db.get(StoryProject, project.id).channel_workflow_id == wf.id
    assert db.get(ChannelWorkflow, wf.id).workflow_session_id == session.id


# --- versioning / partial unique dedupe -------------------------------------


def test_one_active_version_per_number_per_project(db):
    project = make_project(db)
    make_version(db, project, number=1)
    with pytest.raises(IntegrityError):
        make_version(db, project, number=1)
    db.rollback()

    # a second project may reuse version 1 freely
    other = make_project(db)
    make_version(db, other, number=1)
    db.commit()


def test_superseding_version_frees_the_number(db):
    project = make_project(db)
    first = make_version(db, project, number=1)
    first.status = VersionStatus.SUPERSEDED.value
    db.commit()
    make_version(db, project, number=1)  # slot freed


def test_abandoned_version_frees_the_number(db):
    project = make_project(db)
    make_version(db, project, number=2, status="abandoned")
    make_version(db, project, number=2)  # abandoned is not active
    db.commit()


def test_version_numbers_are_per_project_sequential(db):
    project = make_project(db)
    make_version(db, project, number=1)
    make_version(db, project, number=2)
    make_version(db, project, number=3)
    count = db.scalar(select(func.count(StoryVersion.id)).where(StoryVersion.story_project_id == project.id))
    assert count == 3


def test_snapshot_number_dedupe_per_project(db):
    project = make_project(db)
    make_snapshot(db, project, number=1)
    with pytest.raises(IntegrityError):
        make_snapshot(db, project, number=1)
    db.rollback()
    make_snapshot(db, project, number=2)  # next number is fine
    db.commit()


def test_canon_one_active_analysis_per_snapshot(db):
    project = make_project(db)
    snap = make_snapshot(db, project)
    db.add(CanonAnalysis(source_snapshot_id=snap.id, status="queued"))
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(CanonAnalysis(source_snapshot_id=snap.id, status="processing"))
        db.commit()
    db.rollback()

    db.scalars(
        select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snap.id)
        .execution_options(populate_existing=True)
    ).all()[0].status = DomainStatus.COMPLETED.value
    db.commit()
    db.add(CanonAnalysis(source_snapshot_id=snap.id, status="queued"))  # completed freed the slot
    db.commit()


def test_audio_run_and_chunk_dedupe(db):
    project = make_project(db)
    ver = make_version(db, project, number=1)
    tts = TTSGeneration(story_version_id=ver.id, voice="mien", engine="vieneu", status="completed")
    db.add(tts)
    db.commit()

    audio = AudioGeneration(tts_generation_id=tts.id, run_number=1, status="processing")
    db.add(audio)
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(AudioGeneration(tts_generation_id=tts.id, run_number=1, status="queued"))
        db.commit()
    db.rollback()

    c0 = AudioChunk(audio_generation_id=audio.id, chunk_index=0, artifact_path="p/0.wav", duration_ms=100)
    db.add(c0)
    db.commit()
    with pytest.raises(IntegrityError):
        db.add(AudioChunk(audio_generation_id=audio.id, chunk_index=0, artifact_path="p/0-dup.wav"))
        db.commit()
    db.rollback()
    db.add(AudioChunk(audio_generation_id=audio.id, chunk_index=1, artifact_path="p/1.wav"))
    db.commit()


# --- artifact-backed content -------------------------------------------------


def test_version_content_stored_via_artifact_store(db, tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    project = make_project(db)
    ver = make_version(db, project, number=1, status="active")
    rel = store.write(f"projects/{project.id}/versions/{ver.version_number}.md", b"chapter one")
    ver.content_path = rel
    db.commit()

    assert store.exists(rel)
    assert store.read(rel) == b"chapter one"
    assert db.get(StoryVersion, ver.id).content_path == rel