"""Opt-in REAL smoke: synthesize a tiny Vietnamese story with the locally installed VieNeu-TTS.

Skipped unless STORYFLOW_RUN_REAL_SMOKE=1, STORYFLOW_TTS_ENGINE=vieneu and STORYFLOW_VIENEU_ROOT is set.
Loads the real model in the VieNeu venv (subprocess), ~15 s load + a few seconds of synthesis; no network
is required by StoryFlow itself. Runs TTSStep + AudioStep through the real Dispatcher.
"""

import io
import os
import time
import wave
from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow import tts_steps
from storyflow.agents import AgentRunner, RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher
from storyflow.integrations.vieneu import VieNeuPipelineRunner, VieNeuProvider, pcm_wav_duration_ms
from storyflow.models import AudioChunk, ChannelWorkflow, RunnerInstance, StoryProject, WorkflowSession
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import OutputValidatingRunner, PipelineContext
from storyflow.providers import READY, load_provider_config
from storyflow.roles import Role
from storyflow.story_steps import STORY_VALIDATORS, CanonStep, FakeStoryPipelineRunner, SourceStep, StoryStep
from storyflow.subtitles import FakeSubtitleClient
from storyflow.tts_steps import TTS_VALIDATORS, AudioStep, TTSStep

pytestmark = pytest.mark.skipif(
    os.environ.get("STORYFLOW_RUN_REAL_SMOKE") != "1" or os.environ.get("STORYFLOW_TTS_ENGINE") != "vieneu"
    or not os.environ.get("STORYFLOW_VIENEU_ROOT"),
    reason="set STORYFLOW_RUN_REAL_SMOKE=1, STORYFLOW_TTS_ENGINE=vieneu and STORYFLOW_VIENEU_ROOT to run real TTS")

STORY = ("Đêm qua, người canh hải đăng thắp đèn như mọi khi. "
         "Sáng nay, con tàu mất tích đã trở về bến.")
NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "Vietnamese", "language_code": "vi", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "Hải đăng.", "start": 0.0}]}


class VietnameseStory(FakeStoryPipelineRunner):
    def _story(self, packet):
        return (STORY + "\n").encode("utf-8")


class Router(AgentRunner):
    runner_type = "vieneu"

    def __init__(self, store, config):
        self.story = VietnameseStory(store)
        self.vieneu = VieNeuPipelineRunner(config, store)
        self.vieneu.tts.chunking = {"preferred_chunk_chars_max": 60, "avoid_chunk_below_chars": 10}

    def execute(self, packet):
        return (self.vieneu if packet.task_config["step"] in ("tts", "audio") else self.story).execute(packet)

    def classify_error(self, error):
        return self.vieneu.classify_error(error)


def test_real_vieneu_synthesizes_a_two_sentence_story(session_factory, tmp_path, monkeypatch, capsys):
    config = load_provider_config()
    status = VieNeuProvider(config, ArtifactStore(tmp_path / "probe")).status()
    if status.state != READY:
        pytest.skip(f"VieNeu unavailable: {status.message}")

    store = ArtifactStore(tmp_path / "artifacts")
    ctx = PipelineContext(session_factory=session_factory, store=store,
                          subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}), clock=lambda: NOW)
    db = session_factory()
    sess = WorkflowSession(mode="auto", status="active", role_preferences={})
    db.add(sess)
    db.commit()
    wf = ChannelWorkflow(workflow_session_id=sess.id, name="chan", mode="auto", config={
        "source": {"video_id": "vid", "languages": ["vi"]}, "story": {"branch": "hai dang"},
        "tts": {"voice": config.vieneu_voice}})
    db.add(wf)
    db.commit()
    db.add(StoryProject(channel_workflow_id=wf.id, title="Hai dang", slug="hai-dang"))
    db.commit()
    inst = RunnerInstance(workflow_session_id=sess.id, runner_type="vieneu", max_concurrency=1,
                          supported_roles=[Role.STORY_WRITER.value, Role.TTS_ADAPTER.value])
    db.add(inst)
    db.commit()
    workflow_id, instance_id = wf.id, inst.id
    db.close()
    registry = RunnerRegistry()
    registry.register(instance_id, OutputValidatingRunner(Router(store, config), store,
                                                          {**STORY_VALIDATORS, **TTS_VALIDATORS}))

    started = time.monotonic()
    result = Orchestrator(ctx, Dispatcher(registry), SourceStep(),
                          [CanonStep(), StoryStep(), TTSStep(), AudioStep()]).run_until_idle(workflow_id)
    elapsed = time.monotonic() - started
    assert result.status == "completed", result

    db = session_factory()
    chunks = db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index)).all()
    db.close()
    assert len(chunks) >= 1 and [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    report = []
    for c in chunks:
        data = store.read(c.artifact_path)
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        with wave.open(io.BytesIO(data)) as w:
            rate, channels, width, frames = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        assert channels == 1 and width == 2 and rate >= 16000 and frames > 0
        assert c.duration_ms == frames * 1000 // rate > 0
        report.append(f"chunk {c.chunk_index}: {rate} Hz, {c.duration_ms} ms, {len(data)} bytes")
    assert not list(store.root.rglob("*.part")) and not list(store.root.rglob(".tmp-*"))
    with capsys.disabled():
        print(f"\n[real-tts] total {elapsed:.1f}s; " + "; ".join(report))
