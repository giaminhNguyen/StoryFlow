"""The read model points at the single joined audio file (final.wav) once the audio run is complete."""

import wave

import pytest

from test_phase5_integration import Stack


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.app.close()


def _finished_project(stack):
    wf = stack.new_workflow()
    stack.workflows.start(wf)
    stack.assign_discovered_runner(wf)
    snap = stack.drive(wf, lambda s: s.display_state == "completed")
    return snap.projects[0].id


def test_completed_audio_exposes_the_final_file(stack):
    pid = _finished_project(stack)
    audio = stack.read.get_project(pid).audio
    assert audio.status == "completed" and audio.chunk_count > 0
    assert audio.final_path == f"{audio.store_dir}/final.wav"
    assert audio.final_path.startswith("projects/") and ".." not in audio.final_path
    with wave.open(str(stack.app.store.resolve(audio.final_path)), "rb") as w:   # a real, readable wav
        assert w.getnframes() > 0
        chunk_frames = 0
        for c in audio.chunks:
            with wave.open(str(stack.app.store.resolve(c.artifact_path)), "rb") as cw:
                chunk_frames += cw.getnframes()
        assert w.getnframes() >= chunk_frames        # all chunks plus the pauses between them


def test_a_missing_final_file_is_not_exposed(stack):
    pid = _finished_project(stack)
    audio = stack.read.get_project(pid).audio
    stack.app.store.delete(audio.final_path)
    assert stack.read.get_project(pid).audio.final_path is None


def test_final_path_rules_for_unfinished_or_unsafe_runs(stack):
    from types import SimpleNamespace
    pid = _finished_project(stack)
    audio = stack.read.get_project(pid).audio
    rm = stack.read
    row = lambda status, store_dir: SimpleNamespace(status=status, store_dir=store_dir)   # noqa: E731
    assert rm._final_audio_path(row("completed", audio.store_dir)) == audio.final_path
    assert rm._final_audio_path(row("processing", audio.store_dir)) is None      # a run still in progress
    assert rm._final_audio_path(row("queued", audio.store_dir)) is None
    assert rm._final_audio_path(row("completed", None)) is None
    assert rm._final_audio_path(row("completed", "../outside")) is None           # unsafe directory is never exposed
    assert rm._final_audio_path(row("completed", "projects/nope/audio/x/run-001")) is None   # no such file
