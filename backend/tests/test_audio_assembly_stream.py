"""assemble_final_audio streams (constant memory, atomic, no partial / stray files) and ensure_final_audio rebuilds
only when needed; failures are logged without absolute paths."""

import io
import logging
import os
import tracemalloc
import wave

import pytest

from storyflow import tts_steps
from storyflow.artifacts import ArtifactStore
from storyflow.tts_steps import (
    CHUNK_GAP_SECONDS, FINAL_AUDIO_NAME, assemble_final_audio, ensure_final_audio,
)

RUN_DIR = "projects/p1/audio/g1/run-001"


def pcm(frames: int, rate: int = 24000, width: int = 2, value: int = 700) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes((value if width == 2 else 0x90).to_bytes(width, "little", signed=width == 2) * frames)
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def write_chunks(store, n, frames=1000, **kw):
    for i in range(1, n + 1):
        store.write(f"{RUN_DIR}/{i:04d}.wav", pcm(frames, **kw))


def files_in_run_dir(store):
    return sorted(p.name for p in store.resolve(RUN_DIR).iterdir())


def final_frames(store):
    with wave.open(str(store.resolve(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")), "rb") as w:
        return w.getnframes()


# --- streaming --------------------------------------------------------------------------------


def test_memory_stays_constant_for_a_long_story(store):
    n, frames = 24, 650_000                       # 24 chunks x ~1.3 MB = ~31 MB of audio
    write_chunks(store, n, frames)
    total_bytes = n * frames * 2
    tracemalloc.start()
    try:
        rel = assemble_final_audio(store, RUN_DIR, n)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rel == f"{RUN_DIR}/{FINAL_AUDIO_NAME}"
    assert final_frames(store) == n * frames + (n - 1) * int(24000 * CHUNK_GAP_SECONDS)
    assert total_bytes > 30_000_000 and peak < 3_000_000, f"peak {peak} bytes for {total_bytes} bytes of audio"


def test_output_is_byte_identical_to_the_expected_join(store):
    write_chunks(store, 3, frames=500)
    assemble_final_audio(store, RUN_DIR, 3)
    with wave.open(str(store.resolve(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")), "rb") as w:
        data = w.readframes(w.getnframes())
    gap = bytes(int(24000 * CHUNK_GAP_SECONDS) * 2)
    body = (700).to_bytes(2, "little", signed=True) * 500
    assert data == body + gap + body + gap + body


def test_8bit_gap_is_centred_on_0x80(store):
    write_chunks(store, 2, frames=100, rate=8000, width=1)
    assemble_final_audio(store, RUN_DIR, 2)
    with wave.open(str(store.resolve(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")), "rb") as w:
        data = w.readframes(w.getnframes())
    gap = int(8000 * CHUNK_GAP_SECONDS)
    assert set(data[100:100 + gap]) == {0x80} and len(data) == 200 + gap


# --- never a partial or stray file ---------------------------------------------------------------


def test_mismatched_formats_write_nothing(store):
    store.write(f"{RUN_DIR}/0001.wav", pcm(100, rate=24000))
    store.write(f"{RUN_DIR}/0002.wav", pcm(100, rate=48000))
    assert assemble_final_audio(store, RUN_DIR, 2) is None
    assert files_in_run_dir(store) == ["0001.wav", "0002.wav"]          # no final.wav, no temp file


def test_a_failure_halfway_leaves_no_final_and_no_temp_file(store, monkeypatch):
    write_chunks(store, 4)
    real = wave.Wave_read.readframes
    calls = {"n": 0}

    def flaky(self, n):
        calls["n"] += 1
        if calls["n"] == 3:
            raise EOFError("boom in the middle")
        return real(self, n)

    monkeypatch.setattr(wave.Wave_read, "readframes", flaky)
    assert assemble_final_audio(store, RUN_DIR, 4) is None
    assert calls["n"] >= 3
    assert files_in_run_dir(store) == ["0001.wav", "0002.wav", "0003.wav", "0004.wav"]


def test_a_failure_keeps_a_previous_final_file_intact(store, monkeypatch):
    write_chunks(store, 2)
    assert assemble_final_audio(store, RUN_DIR, 2)
    before = store.read(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")
    monkeypatch.setattr(tts_steps.os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    assert assemble_final_audio(store, RUN_DIR, 2) is None
    assert store.read(f"{RUN_DIR}/{FINAL_AUDIO_NAME}") == before
    assert files_in_run_dir(store) == ["0001.wav", "0002.wav", FINAL_AUDIO_NAME]      # the temp file is gone


def test_a_missing_or_corrupt_chunk_writes_nothing(store):
    write_chunks(store, 2)
    assert assemble_final_audio(store, RUN_DIR, 3) is None                      # chunk 3 does not exist
    store.write(f"{RUN_DIR}/0002.wav", b"not a wav at all")
    assert assemble_final_audio(store, RUN_DIR, 2) is None
    assert files_in_run_dir(store) == ["0001.wav", "0002.wav"]
    assert assemble_final_audio(store, RUN_DIR, 0) is None


# --- visible failures -----------------------------------------------------------------------------


def test_failures_are_logged_with_the_relative_dir_and_no_absolute_path(store, caplog, tmp_path):
    write_chunks(store, 2)
    store.write(f"{RUN_DIR}/0002.wav", b"not a wav at all")
    with caplog.at_level(logging.WARNING, logger="storyflow.tts_steps"):
        assert assemble_final_audio(store, RUN_DIR, 2) is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "final audio not built" in text and RUN_DIR in text
    assert str(tmp_path) not in text and "\\" not in text


def test_a_permission_error_is_logged_without_leaking_the_path(store, caplog, monkeypatch, tmp_path):
    write_chunks(store, 2)

    def deny(*a, **k):
        raise PermissionError(13, "Permission denied", str(store.root / "secret" / "x.wav"))

    monkeypatch.setattr(tts_steps.os, "replace", deny)
    with caplog.at_level(logging.WARNING, logger="storyflow.tts_steps"):
        assert assemble_final_audio(store, RUN_DIR, 2) is None
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "PermissionError" in text and "Permission denied" in text and str(tmp_path) not in text


def test_a_format_mismatch_is_logged(store, caplog):
    store.write(f"{RUN_DIR}/0001.wav", pcm(10, rate=24000))
    store.write(f"{RUN_DIR}/0002.wav", pcm(10, rate=16000))
    with caplog.at_level(logging.WARNING, logger="storyflow.tts_steps"):
        assemble_final_audio(store, RUN_DIR, 2)
    assert any("differ in audio format" in r.getMessage() for r in caplog.records)


# --- ensure_final_audio ----------------------------------------------------------------------------


def test_ensure_builds_when_missing_and_keeps_an_up_to_date_file(store, monkeypatch):
    write_chunks(store, 2)
    final = store.resolve(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")
    assert ensure_final_audio(store, RUN_DIR, 2) == f"{RUN_DIR}/{FINAL_AUDIO_NAME}" and final.is_file()
    stamp = final.stat().st_mtime_ns
    monkeypatch.setattr(tts_steps, "assemble_final_audio", lambda *a: (_ for _ in ()).throw(AssertionError("rebuilt")))
    assert ensure_final_audio(store, RUN_DIR, 2) == f"{RUN_DIR}/{FINAL_AUDIO_NAME}"      # up to date: untouched
    assert final.stat().st_mtime_ns == stamp


def test_ensure_rebuilds_when_a_chunk_is_newer_than_the_final_file(store):
    write_chunks(store, 2, frames=1000)
    ensure_final_audio(store, RUN_DIR, 2)
    first = final_frames(store)
    final = store.resolve(f"{RUN_DIR}/{FINAL_AUDIO_NAME}")
    store.write(f"{RUN_DIR}/0002.wav", pcm(3000))                     # a regenerated chunk
    newer = final.stat().st_mtime_ns + 5_000_000_000
    os.utime(store.resolve(f"{RUN_DIR}/0002.wav"), ns=(newer, newer))
    assert ensure_final_audio(store, RUN_DIR, 2)
    assert final_frames(store) == first + 2000


def test_ensure_returns_none_when_a_chunk_is_missing(store):
    write_chunks(store, 1)
    assert ensure_final_audio(store, RUN_DIR, 2) is None
    assert files_in_run_dir(store) == ["0001.wav"]


def test_audio_finalize_uses_ensure_final_audio():
    import inspect
    src = inspect.getsource(tts_steps.AudioStep.finalize)
    assert "ensure_final_audio(" in src and "assemble_final_audio(" not in src
