"""Final audio assembly (single final.wav per audio run) and the story length policy
(default target = at least the source length; too-short stories are rejected)."""

import io
import wave
from datetime import datetime

import pytest

from storyflow.artifacts import ArtifactStore
from storyflow.models import ChannelWorkflow, StoryProject
from storyflow.pipeline import PipelineContext
from storyflow.story_steps import (
    MIN_LENGTH_RATIO, SourceStep, StoryStep, check_story_text,
)
from storyflow.subtitles import FakeSubtitleClient
from storyflow.tts_steps import CHUNK_GAP_SECONDS, FINAL_AUDIO_NAME, assemble_final_audio, fake_wav

NOW = datetime(2026, 1, 2, 12, 0, 0)
RUN_DIR = "projects/p1/audio/g1/run-001"


def pcm16(n_frames: int, rate: int = 24000, value: int = 1000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(value.to_bytes(2, "little", signed=True) * n_frames)
    return buf.getvalue()


def frames_of(store, rel):
    with wave.open(str(store.resolve(rel)), "rb") as w:
        return w.getnframes(), w.getframerate(), w.getsampwidth(), w.readframes(w.getnframes())


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# --- assemble_final_audio -----------------------------------------------------------------


def test_assemble_joins_chunks_in_order_with_gap(store):
    lengths = [1000, 2000, 3000]
    for i, n in enumerate(lengths, 1):
        store.write(f"{RUN_DIR}/{i:04d}.wav", pcm16(n, value=i * 100))
    rel = assemble_final_audio(store, RUN_DIR, 3)
    assert rel == f"{RUN_DIR}/{FINAL_AUDIO_NAME}"
    n, rate, width, data = frames_of(store, rel)
    gap = int(24000 * CHUNK_GAP_SECONDS)
    assert (rate, width) == (24000, 2)
    assert n == sum(lengths) + 2 * gap
    samples = [int.from_bytes(data[i:i + 2], "little", signed=True) for i in range(0, len(data), 2)]
    assert samples[0] == 100                                   # chunk 1 first
    assert samples[lengths[0]] == 0                            # 16-bit silence is 0
    assert samples[lengths[0] + gap] == 200                    # then chunk 2
    assert samples[-1] == 300                                  # chunk 3 last


def test_assemble_8bit_silence_is_centred_on_0x80(store):
    for i in (1, 2):
        store.write(f"{RUN_DIR}/{i:04d}.wav", fake_wav(f"chunk {i}"))
    rel = assemble_final_audio(store, RUN_DIR, 2)
    _, rate, width, data = frames_of(store, rel)
    assert width == 1 and rate == 8000
    gap = int(rate * CHUNK_GAP_SECONDS)
    first = len(fake_wav("chunk 1")) - 44
    assert set(data[first:first + gap]) == {0x80}


def test_assemble_is_idempotent(store):
    for i in (1, 2):
        store.write(f"{RUN_DIR}/{i:04d}.wav", pcm16(500))
    a = assemble_final_audio(store, RUN_DIR, 2)
    first = frames_of(store, a)
    assert assemble_final_audio(store, RUN_DIR, 2) == a
    assert frames_of(store, a) == first


def test_assemble_returns_none_when_a_chunk_is_missing(store):
    store.write(f"{RUN_DIR}/0001.wav", pcm16(500))
    assert assemble_final_audio(store, RUN_DIR, 2) is None
    assert not store.exists(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")


def test_assemble_returns_none_for_mismatched_formats(store):
    store.write(f"{RUN_DIR}/0001.wav", pcm16(500, rate=24000))
    store.write(f"{RUN_DIR}/0002.wav", pcm16(500, rate=48000))
    assert assemble_final_audio(store, RUN_DIR, 2) is None


def test_assemble_returns_none_for_corrupt_chunk(store):
    store.write(f"{RUN_DIR}/0001.wav", b"not a wav")
    assert assemble_final_audio(store, RUN_DIR, 1) is None


# --- check_story_text length policy -------------------------------------------------------


def words(n):
    return " ".join(["từ"] * n)


def test_story_without_target_only_needs_a_few_words():
    assert check_story_text(words(6)) is None


def test_story_shorter_than_ratio_is_rejected():
    limit = int(1000 * MIN_LENGTH_RATIO)
    assert "too short" in check_story_text(words(limit - 1), target_length=1000)
    assert check_story_text(words(limit), target_length=1000) is None
    assert check_story_text(words(1500), target_length=1000) is None


@pytest.mark.parametrize("bad", [None, 0, -5, "1000", True, 12.5])
def test_non_positive_or_non_int_target_is_ignored(bad):
    assert check_story_text(words(10), target_length=bad) is None


# --- default target_length = source length --------------------------------------------------

TRACK = {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "một hai ba bốn năm", "start": 0.0}, {"text": "sáu bảy tám", "start": 1.0}]}


def _ctx(session_factory, store):
    return PipelineContext(session_factory=session_factory, store=store,
                           subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}), clock=lambda: NOW)


def _project(db, story_cfg):
    wf = ChannelWorkflow(name="c", mode="auto", status="active",
                         config={"source": {"video_id": "vid", "languages": ["vi"]}, "story": story_cfg})
    db.add(wf)
    db.commit()
    p = StoryProject(title="t", channel_workflow_id=wf.id)
    db.add(p)
    db.commit()
    return p


def _story_begin(db, ctx, project):
    """Run source, then a canon domain row marked completed (no runner needed), then StoryStep.begin."""
    from storyflow.models import CanonAnalysis, DomainStatus, SourceSnapshot
    from sqlalchemy import select
    assert SourceStep().run(ctx, project.id).status.value == "completed"
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project.id))
    db.add(CanonAnalysis(source_snapshot_id=snap.id, status=DomainStatus.COMPLETED.value, canon={"x": 1}))
    db.commit()
    return snap, StoryStep().begin(db, ctx, project)


def test_default_target_length_is_the_source_word_count(db, session_factory, store):
    ctx = _ctx(session_factory, store)
    project = _project(db, {})
    snap, begun = _story_begin(db, ctx, project)
    _, spec = begun
    source_words = len(store.read(snap.meta["artifact_path"]).decode("utf-8").split())
    assert source_words == 8
    assert spec.payload["inputs"]["target_length"] == source_words
    assert spec.payload["task_config"]["target_length"] == source_words


def test_explicit_target_length_wins_over_source_length(db, session_factory, store):
    ctx = _ctx(session_factory, store)
    project = _project(db, {"target_length": 5000})
    _, begun = _story_begin(db, ctx, project)
    assert begun[1].payload["inputs"]["target_length"] == 5000
