"""Phase 4 TTS + audio pipeline steps (workstream 3): ``TTSStep`` and ``AudioStep``.

Audit of the pinned skill ``skills/story-tts-adapter`` (read-only; never modified here)
------------------------------------------------------------------------------------
Skill workflow -> StoryFlow tasks
  A.1-A.5  load story, load profile, one TTS adaptation pass, save ``story_tts.txt``
  A.6      ``scripts/chunk_tts.py``  -> ``chunks/NNNN.txt`` + ``manifest.json``
  A.7      ``scripts/validate_tts_output.py`` (profile id, files exist, hard limit)
  ==> ALL of A.1-A.7 is ONE runner job, step ``tts`` (role ``tts_adapter``), domain row
      ``TTSGeneration``. The StoryFlow-side equivalent of A.7 is ``validate_tts_artifacts``
      (run both as ``OutputValidatingRunner`` validator and again in ``finalize``).
  ``synthesis.preferred_input_mode`` (profile) says the engine may take ``story_tts.txt``
  directly; StoryFlow always synthesises per chunk (external_chunks fallback) because audio
  progress/resume is tracked per chunk. This is step ``audio`` (role ``tts_adapter``,
  domain rows ``AudioGeneration`` + ``AudioChunk``); the skill itself has no synthesis part.

Adapter contract
  inputs : story (StoryVersion.content_path), tts_profile id (registry.json; the bundled
           default is ``vieneu-v3-turbo-story``; a missing id is an error, never substituted).
  outputs: <out>/story_tts.txt, <out>/manifest.json, <out>/chunks/0001.txt ... (contiguous,
           4 digit). manifest keys used: profile_id, source_story, adapted_story,
           total_chars, chunk_count, hard_max_chars, chunks[{index,file,chars}].
           StoryFlow additionally requires ``source_story_version_id`` in the manifest so a
           manifest can never be attached to a different (or later edited) StoryVersion.
  The job payload lists only story_tts.txt + manifest.json; chunks are enumerated by the
  manifest (their count is unknown before adaptation). The manifest is written LAST, it is
  the commit marker of the adaptation.

DOCUMENTED GAPS (executable contract = the fakes below)
  * No production TTS synthesis backend: VieNeu (or any engine) is not wired.
  * No real AI runner for the ``tts_adapter`` role yet: the AionUI task-run gap described in
    gateway.py's docstring still applies, so the semantic adaptation pass (A.3) is not
    performed by anything real. ``FakeTTSAdapterRunner`` only normalises whitespace/markdown.
  * The skill scripts are NOT shelled out to from the default path. ``FakeTTSAdapterRunner``
    re-implements the deterministic behaviour of chunk_tts.py (sentence based, hard limit
    from the profile, tiny-chunk merge) with one deliberate deviation: an over-long unit is
    split by ``split_hard`` into chunks directly (the script can overwrite its accumulator).
  * Audio uses a fake 8 kHz 8-bit mono PCM wav (deterministic bytes, header-checkable).
  * Concurrency: begin() is guarded by DB unique indexes / a single INSERT..SELECT..WHERE NOT
    EXISTS statement; DB access assumes SQLite (as the rest of the backend).

Persistence rules: DB stores RELATIVE artifact paths only; every artifact write goes through
ArtifactStore.write (temp + fsync + atomic promote); completed output is never overwritten.
"""

import hashlib
import json
import re
import struct
from collections import deque
from functools import lru_cache

from sqlalchemy import DateTime, exists, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError

from .agents import AgentRunner, CRASH, TIMEOUT, _CrashSentinel
from .artifacts import ArtifactStore
from .config import PROJECT_ROOT
from .models import (
    AudioChunk,
    AudioGeneration,
    DomainStatus,
    PipelineJob,
    StoryProject,
    StoryVersion,
    TTSGeneration,
    VersionStatus,
    uid,
)
from .pipeline import JobSpec, PipelineContext, StepHandler, StepStatus, StepView, workflow_config
from .protocol import ResultCode, RunnerResult, TaskPacket
from .roles import Role

SKILL_NAME = "story-tts-adapter"
SKILL_PATH = "skills/story-tts-adapter"
DEFAULT_VOICE = "default"
DEFAULT_ENGINE = "vieneu-tts-v3-turbo"
LIVE = (DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value, DomainStatus.COMPLETED.value)
IN_FLIGHT = (DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value)
_FRESH = {"execution_options": {"populate_existing": True}}


class UnknownProfile(ValueError):
    """Requested TTS profile id is not in the skill's registry (never silently substituted)."""


# --- skill / profile metadata -------------------------------------------------------------


def _profiles_dir():
    return PROJECT_ROOT / "skills" / SKILL_NAME / "references" / "profiles"


@lru_cache(maxsize=None)
def _registry() -> dict:
    return json.loads((_profiles_dir() / "registry.json").read_text(encoding="utf-8"))


def default_profile_id() -> str:
    return _registry()["default_profile"]


@lru_cache(maxsize=None)
def load_profile(profile_id: str) -> dict:
    fname = _registry()["profiles"].get(profile_id)
    if fname is None:
        raise UnknownProfile(profile_id)
    return json.loads((_profiles_dir() / fname).read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def skill_revision() -> str | None:
    lock = json.loads((PROJECT_ROOT / "sources.lock.json").read_text(encoding="utf-8"))
    return lock["skills"]["projects"][SKILL_NAME]["revision"]


def tts_config(db, project: StoryProject) -> dict:
    cfg = workflow_config(db, project).get("tts") or {}
    return {
        "voice": cfg.get("voice") or DEFAULT_VOICE,
        "engine": cfg.get("engine") or DEFAULT_ENGINE,
        "profile": cfg.get("profile") or default_profile_id(),
    }


def tts_dir(project_id: str, tts_generation_id: str) -> str:
    return f"projects/{project_id}/tts/{tts_generation_id}"


def audio_dir(project_id: str, tts_generation_id: str, run_number: int) -> str:
    return f"projects/{project_id}/audio/{tts_generation_id}/run-{run_number:03d}"


def _skill_block() -> dict:
    return {"name": SKILL_NAME, "path": SKILL_PATH, "revision": skill_revision()}


# --- fake wav ---------------------------------------------------------------------------------

WAV_RATE = 8000          # 8 kHz, 8-bit mono => 8 bytes per millisecond
_WAV_HEADER = 44


def fake_wav(text: str) -> bytes:
    """Deterministic fake wav: duration = 20 ms per character, payload derived from sha256(text)."""
    duration_ms = max(20, len(text) * 20)
    n = duration_ms * WAV_RATE // 1000
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    data = (seed * (n // len(seed) + 1))[:n]
    header = (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE" + b"fmt " +
              struct.pack("<IHHIIHH", 16, 1, 1, WAV_RATE, WAV_RATE, 1, 8) + b"data" + struct.pack("<I", n))
    return header + data


def wav_duration_ms(data: bytes) -> int | None:
    """Duration of a fake/PCM wav, or None if the bytes are not a well-formed non-empty wav."""
    if len(data) <= _WAV_HEADER or data[:4] != b"RIFF" or data[8:12] != b"WAVE" or data[12:16] != b"fmt ":
        return None
    fmt, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", data[20:36])
    if fmt != 1 or channels != 1 or bits != 8 or rate != WAV_RATE or data[36:40] != b"data":
        return None
    (size,) = struct.unpack("<I", data[40:44])
    if size == 0 or size != len(data) - _WAV_HEADER:
        return None
    return size * 1000 // WAV_RATE


def _valid_audio(store: ArtifactStore, path: str) -> int | None:
    """duration_ms when `path` exists and is a valid wav, else None."""
    if not store.exists(path):
        return None
    return wav_duration_ms(store.read(path))


# --- adapter output validation (StoryFlow-side equivalent of validate_tts_output.py) ---------


def _read_manifest(store: ArtifactStore, out_dir: str):
    path = f"{out_dir}/manifest.json"
    if not store.exists(path):
        return None, "manifest.json missing"
    try:
        return json.loads(store.read(path).decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        return None, "manifest.json is not valid JSON"


def validate_tts_artifacts(store: ArtifactStore, out_dir: str, *, story_version_id: str,
                           profile_id: str):
    """Returns (problem | None, manifest | None). Deterministic, read-only."""
    manifest, problem = _read_manifest(store, out_dir)
    if problem:
        return problem, None
    if not isinstance(manifest, dict):
        return "manifest is not an object", None
    try:
        hard = int(load_profile(profile_id)["chunking"]["hard_max_chars"])
    except UnknownProfile:
        return f"unknown profile {profile_id}", None
    if manifest.get("profile_id") != profile_id:
        return "manifest profile_id does not match the requested profile", None
    if manifest.get("source_story_version_id") != story_version_id:
        return "manifest source_story_version_id does not match the generation's StoryVersion", None
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return "manifest has no chunks", None
    if manifest.get("chunk_count") != len(chunks):
        return "manifest chunk_count does not match chunks list", None
    if not store.exists(f"{out_dir}/story_tts.txt") or not store.read(f"{out_dir}/story_tts.txt").strip():
        return "story_tts.txt missing or empty", None
    for i, rec in enumerate(chunks, 1):
        if not isinstance(rec, dict) or rec.get("index") != i or rec.get("file") != f"chunks/{i:04d}.txt":
            return f"chunk {i}: index/filename not contiguous", None
        rel = f"{out_dir}/{rec['file']}"
        if not store.exists(rel):
            return f"chunk {i}: file missing", None
        text = store.read(rel).decode("utf-8", errors="replace").strip()
        if not text:
            return f"chunk {i}: empty text", None
        if len(text) > hard:
            return f"chunk {i}: exceeds hard limit {len(text)} > {hard}", None
    return None, manifest


def _chunk_text(store: ArtifactStore, out_dir: str, rec: dict) -> str:
    return store.read(f"{out_dir}/{rec['file']}").decode("utf-8").strip()


def validate_tts_output(packet: TaskPacket, store: ArtifactStore):
    i = packet.inputs
    problem, _ = validate_tts_artifacts(store, i["output_dir"], story_version_id=i["story_version_id"],
                                        profile_id=i["profile"])
    return problem


def validate_audio_output(packet: TaskPacket, store: ArtifactStore):
    for path in packet.outputs:
        if _valid_audio(store, path) is None:
            return f"invalid or missing audio file {path}"
    return None


TTS_VALIDATORS = {"tts": validate_tts_output, "audio": validate_audio_output}


# --- shared handler helpers -------------------------------------------------------------------


def _latest_version(db, project: StoryProject) -> StoryVersion | None:
    return db.scalar(
        select(StoryVersion)
        .where(StoryVersion.story_project_id == project.id, StoryVersion.status == VersionStatus.ACTIVE.value)
        .order_by(StoryVersion.version_number.desc()).limit(1), **_FRESH)


def _fail(row, job: PipelineJob, now):
    if row.status in IN_FLIGHT:
        row.status = DomainStatus.FAILED.value
        row.error_code = job.last_error_code or "job_failed"
        row.error_message = job.last_error_message
        row.finished_at = now
        row.updated_at = now


# --- TTS step ---------------------------------------------------------------------------------


class TTSStep(StepHandler):
    step = "tts"
    job_kind = "tts_generation"
    role = Role.TTS_ADAPTER.value

    def _rows(self, db, version, cfg):
        return db.scalars(
            select(TTSGeneration).where(
                TTSGeneration.story_version_id == version.id, TTSGeneration.voice == cfg["voice"],
                TTSGeneration.engine == cfg["engine"]).order_by(TTSGeneration.created_at.desc()), **_FRESH).all()

    def status(self, db, ctx, project):
        version = _latest_version(db, project)
        if version is None:
            return StepView(StepStatus.NOT_STARTED)
        rows = self._rows(db, version, tts_config(db, project))
        live = next((r for r in rows if r.status in LIVE), None)
        if live is not None:
            st = StepStatus.COMPLETED if live.status == DomainStatus.COMPLETED.value else StepStatus.IN_PROGRESS
            return StepView(st, live.id, live.pipeline_job_id)
        if rows:
            return StepView(StepStatus.FAILED, rows[0].id, rows[0].pipeline_job_id, rows[0].error_code)
        return StepView(StepStatus.NOT_STARTED)

    def begin(self, db, ctx, project):
        version = _latest_version(db, project)
        if version is None or not version.content_path:
            return None
        ctx.store.resolve(version.content_path)  # relative + traversal-safe, else raises
        cfg = tts_config(db, project)
        load_profile(cfg["profile"])  # UnknownProfile if not registered
        row = next((r for r in self._rows(db, version, cfg) if r.status in LIVE), None)
        if row is None:
            now = ctx.clock()
            db.add(TTSGeneration(
                story_version_id=version.id, status=DomainStatus.QUEUED.value, voice=cfg["voice"],
                engine=cfg["engine"], config={"profile": cfg["profile"]}, created_at=now, updated_at=now))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = next((r for r in self._rows(db, version, cfg) if r.status in LIVE), None)
            if row is None:
                raise RuntimeError("tts generation vanished after unique-index conflict")
        out = tts_dir(project.id, row.id)
        profile = (row.config or {}).get("profile", cfg["profile"])
        spec = JobSpec(
            kind=self.job_kind, role=self.role, dedupe_key=f"tts:{row.id}",
            payload={
                "skill": _skill_block(),
                "inputs": {"story_artifact": version.content_path, "story_version_id": version.id,
                           "profile": profile, "output_dir": out, "tts_generation_id": row.id},
                "outputs": [f"{out}/story_tts.txt", f"{out}/manifest.json"],
                "task_config": {"step": self.step, "voice": row.voice, "engine": row.engine},
            })
        return row.id, spec

    def link_job(self, db, ctx, domain_id, job):
        db.execute(update(TTSGeneration).where(
            TTSGeneration.id == domain_id, TTSGeneration.status.in_(IN_FLIGHT),
            (TTSGeneration.pipeline_job_id.is_(None)) | (TTSGeneration.pipeline_job_id != job.id),
        ).values(pipeline_job_id=job.id, updated_at=ctx.clock()))
        db.commit()

    def finalize(self, db, ctx, domain_id, job):
        row = db.get(TTSGeneration, domain_id, populate_existing=True)
        if row is None or row.status not in IN_FLIGHT:
            return  # completed rows are immutable; failed rows need an explicit new generation
        version = db.get(StoryVersion, row.story_version_id)
        out = tts_dir(version.story_project_id, row.id)
        profile = (row.config or {}).get("profile")
        problem, manifest = validate_tts_artifacts(ctx.store, out, story_version_id=version.id, profile_id=profile)
        now = ctx.clock()
        if problem:
            row.status = DomainStatus.FAILED.value
            row.error_code, row.error_message = "invalid_output", problem[:400]
        else:
            row.status = DomainStatus.COMPLETED.value
            row.config = {**(row.config or {}), "profile": profile, "chunk_count": manifest["chunk_count"]}
            row.error_code = row.error_message = None
        row.finished_at = now
        row.updated_at = now
        db.commit()

    def mark_failed(self, db, ctx, domain_id, job):
        row = db.get(TTSGeneration, domain_id, populate_existing=True)
        if row is not None:
            _fail(row, job, ctx.clock())
            db.commit()


# --- Audio step -------------------------------------------------------------------------------


class AudioStep(StepHandler):
    step = "audio"
    job_kind = "audio_generation"
    role = Role.TTS_ADAPTER.value

    def _tts(self, db, project) -> TTSGeneration | None:
        version = _latest_version(db, project)
        if version is None:
            return None
        cfg = tts_config(db, project)
        return db.scalar(
            select(TTSGeneration).where(
                TTSGeneration.story_version_id == version.id, TTSGeneration.voice == cfg["voice"],
                TTSGeneration.engine == cfg["engine"], TTSGeneration.status == DomainStatus.COMPLETED.value)
            .order_by(TTSGeneration.created_at.desc()).limit(1), **_FRESH)

    def _runs(self, db, tts_id):
        return db.scalars(select(AudioGeneration).where(AudioGeneration.tts_generation_id == tts_id)
                          .order_by(AudioGeneration.run_number.desc()), **_FRESH).all()

    def status(self, db, ctx, project):
        tts = self._tts(db, project)
        if tts is None:
            return StepView(StepStatus.NOT_STARTED)
        runs = self._runs(db, tts.id)
        live = next((r for r in runs if r.status in LIVE), None)
        if live is not None:
            st = StepStatus.COMPLETED if live.status == DomainStatus.COMPLETED.value else StepStatus.IN_PROGRESS
            return StepView(st, live.id)
        if runs:
            return StepView(StepStatus.FAILED, runs[0].id, None, runs[0].error_code)
        return StepView(StepStatus.NOT_STARTED)

    def begin(self, db, ctx, project):
        tts = self._tts(db, project)
        if tts is None:
            return None
        row = next((r for r in self._runs(db, tts.id) if r.status in LIVE), None)
        if row is None:
            self._insert_run(db, ctx, project, tts)
            row = next((r for r in self._runs(db, tts.id) if r.status in LIVE), None)
            if row is None:
                raise RuntimeError("audio generation vanished after insert")
        manifest, problem = _read_manifest(ctx.store, tts_dir(project.id, tts.id))
        if problem:
            raise ValueError(f"completed TTS generation {tts.id} has no readable manifest: {problem}")
        t_dir = tts_dir(project.id, tts.id)
        chunk_paths = [f"{t_dir}/{rec['file']}" for rec in manifest["chunks"]]
        outputs = [f"{row.store_dir}/{i:04d}.wav" for i in range(1, len(chunk_paths) + 1)]
        spec = JobSpec(
            kind=self.job_kind, role=self.role, dedupe_key=f"audio:{row.id}",
            payload={
                "skill": _skill_block(),
                "inputs": {"manifest": f"{t_dir}/manifest.json", "chunks": chunk_paths,
                           "output_dir": row.store_dir, "tts_generation_id": tts.id,
                           "audio_generation_id": row.id, "run_number": row.run_number},
                "outputs": outputs,
                "task_config": {"step": self.step, "chunk_count": len(chunk_paths),
                                "voice": tts.voice, "engine": tts.engine},
            })
        return row.id, spec

    def _insert_run(self, db, ctx, project, tts):
        """Single INSERT..SELECT..WHERE NOT EXISTS(live): atomic in SQLite, so concurrent begin()
        callers (and a just-completed run) can never yield two live runs."""
        nxt = (db.scalar(select(func.max(AudioGeneration.run_number))
                         .where(AudioGeneration.tts_generation_id == tts.id)) or 0) + 1
        now = ctx.clock()
        live = exists().where(AudioGeneration.tts_generation_id == tts.id, AudioGeneration.status.in_(LIVE))
        sel = select(literal(uid()), literal(tts.id), literal(nxt), literal(DomainStatus.QUEUED.value),
                     literal(0), literal(audio_dir(project.id, tts.id, nxt)),
                     literal(now, DateTime()), literal(now, DateTime())).where(~live)
        try:
            db.execute(insert(AudioGeneration).from_select(
                ["id", "tts_generation_id", "run_number", "status", "chunk_count", "store_dir",
                 "created_at", "updated_at"], sel))
            db.commit()
        except IntegrityError:
            db.rollback()

    def link_job(self, db, ctx, domain_id, job):
        # AudioGeneration has no pipeline_job_id column (models are frozen for this phase):
        # the link is derivable via the job dedupe_key ``audio:<id>``. Nothing to persist.
        return None

    # -- chunk registration -----------------------------------------------------------------

    def _load(self, db, ctx, audio_generation_id):
        gen = db.get(AudioGeneration, audio_generation_id, populate_existing=True)
        tts = db.get(TTSGeneration, gen.tts_generation_id)
        version = db.get(StoryVersion, tts.story_version_id)
        t_dir = tts_dir(version.story_project_id, tts.id)
        manifest, problem = _read_manifest(ctx.store, t_dir)
        if problem:
            raise ValueError(f"TTS manifest unreadable: {problem}")
        return gen, t_dir, manifest

    def sync_chunks(self, db, ctx, audio_generation_id) -> list[int]:
        return sync_chunks(db, ctx, audio_generation_id)

    def finalize(self, db, ctx, domain_id, job):
        gen = db.get(AudioGeneration, domain_id, populate_existing=True)
        if gen is None or gen.status not in IN_FLIGHT:
            return
        gen, t_dir, manifest = self._load(db, ctx, domain_id)
        sync_chunks(db, ctx, domain_id)
        total = len(manifest["chunks"])
        have = set(db.scalars(select(AudioChunk.chunk_index).where(
            AudioChunk.audio_generation_id == domain_id, AudioChunk.status == VersionStatus.ACTIVE.value),
            **_FRESH).all())
        gen = db.get(AudioGeneration, domain_id, populate_existing=True)
        now = ctx.clock()
        if have >= set(range(1, total + 1)):
            gen.status = DomainStatus.COMPLETED.value
            gen.chunk_count = total
            gen.error_code = gen.error_message = None
            gen.finished_at = now
        else:  # job claimed success but chunks are missing: stay resumable, make it visible
            gen.error_code = "chunks_missing"
            gen.error_message = f"{total - len(have & set(range(1, total + 1)))} of {total} chunks missing"
        gen.updated_at = now
        db.commit()

    def mark_failed(self, db, ctx, domain_id, job):
        gen = db.get(AudioGeneration, domain_id, populate_existing=True)
        if gen is not None:
            _fail(gen, job, ctx.clock())
            db.commit()


def sync_chunks(db, ctx, audio_generation_id) -> list[int]:
    """Idempotently register AudioChunk rows for chunk files that already exist and are valid.

    Never overwrites or duplicates an existing chunk row; never marks the run completed. A
    queued run with registered chunks becomes ``processing``. Returns newly registered indexes.
    """
    gen = db.get(AudioGeneration, audio_generation_id, populate_existing=True)
    if gen is None or gen.status == DomainStatus.FAILED.value:
        return []
    tts = db.get(TTSGeneration, gen.tts_generation_id)
    version = db.get(StoryVersion, tts.story_version_id)
    t_dir = tts_dir(version.story_project_id, tts.id)
    manifest, problem = _read_manifest(ctx.store, t_dir)
    if problem:
        raise ValueError(f"TTS manifest unreadable: {problem}")
    existing = set(db.scalars(select(AudioChunk.chunk_index).where(
        AudioChunk.audio_generation_id == gen.id, AudioChunk.status == VersionStatus.ACTIVE.value),
        **_FRESH).all())
    added = []
    for i, rec in enumerate(manifest["chunks"], 1):
        if i in existing:
            continue
        path = f"{gen.store_dir}/{i:04d}.wav"
        duration = _valid_audio(ctx.store, path)
        if duration is None:
            continue
        try:
            with db.begin_nested():
                db.add(AudioChunk(audio_generation_id=gen.id, chunk_index=i, artifact_path=path,
                                  duration_ms=duration, text=_chunk_text(ctx.store, t_dir, rec),
                                  created_at=ctx.clock()))
        except IntegrityError:
            continue  # a concurrent syncer registered it first
        added.append(i)
    if added and gen.status == DomainStatus.QUEUED.value:
        gen.status = DomainStatus.PROCESSING.value
        gen.updated_at = ctx.clock()
    db.commit()
    return added


# --- deterministic fake runners ---------------------------------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_RE = re.compile(r"(?<=[,;:])\s+")


def _split_hard(text, limit):
    out, s = [], text.strip()
    while len(s) > limit:
        cut = s.rfind(" ", 0, limit + 1)
        if cut < max(1, limit // 2):
            cut = limit
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def _units(text, hard):
    units = []
    for p in re.split(r"\n\s*\n+", text.strip()):
        p = re.sub(r"\s+", " ", p).strip()
        for s in filter(None, (x.strip() for x in _SENTENCE_RE.split(p))):
            if len(s) <= hard:
                units.append(s)
                continue
            for c in filter(None, (x.strip() for x in _CLAUSE_RE.split(s))):
                units.extend([c] if len(c) <= hard else _split_hard(c, hard))
    return units


def chunk_text(text: str, *, hard: int, preferred_max: int, tiny: int) -> list[str]:
    """Deterministic sentence-based chunker mimicking skills/story-tts-adapter/scripts/chunk_tts.py."""
    chunks, current = [], ""
    for u in _units(text, hard):
        cand = u if not current else current + " " + u
        if len(cand) <= preferred_max:
            current = cand
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(u) <= preferred_max:
            current = u
        else:
            chunks.extend(_split_hard(u, hard))
    if current:
        chunks.append(current)
    if len(chunks) < 2:
        return chunks
    out = []
    for c in chunks:
        if out and len(c) < tiny and len(out[-1]) + 1 + len(c) <= hard:
            out[-1] += " " + c
        else:
            out.append(c)
    if len(out) > 1 and len(out[0]) < tiny and len(out[0]) + 1 + len(out[1]) <= hard:
        out[1] = out[0] + " " + out[1]
        out = out[1:]
    return out


def adapt_text(story: str) -> str:
    """Fake 'adaptation pass': strips markdown markers and collapses whitespace. No semantic edits."""
    text = re.sub(r"^[#>\-\*\s]+", "", story, flags=re.M)
    text = re.sub(r"[*_`]+", "", text)
    paragraphs = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n+", text)]
    return "\n\n".join(p for p in paragraphs if p)


def _check_output_path(store: ArtifactStore, path: str, prefix: str | None = None) -> str:
    """Relative, under projects/, traversal-safe (store.resolve raises PathTraversalError)."""
    if not isinstance(path, str) or not path.startswith("projects/"):
        raise ValueError(f"output path must be relative under projects/: {path!r}")
    store.resolve(path)
    if prefix is not None and not path.startswith(prefix.rstrip("/") + "/"):
        raise ValueError(f"output path {path!r} escapes output dir {prefix!r}")
    return path


class _ScriptedRunner(AgentRunner):
    def __init__(self, store: ArtifactStore, runner_type: str, results):
        self.store = store
        self.runner_type = runner_type
        self._pending = deque(results or [])
        self.invocations: list[TaskPacket] = []

    def _scripted(self, packet):
        """Returns a RunnerResult to short-circuit with, raises for CRASH/TIMEOUT, or None."""
        self.invocations.append(packet)
        if not self._pending:
            return None
        item = self._pending.popleft()
        if isinstance(item, _CrashSentinel):
            raise item.exc
        if item is TIMEOUT:
            raise item
        if isinstance(item, RunnerResult):
            return item
        return None if item is ResultCode.SUCCESS else RunnerResult(code=item)

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED


class FakeTTSAdapterRunner(_ScriptedRunner):
    """Executable contract of the story-tts-adapter job (see module docstring).

    ``chunking`` may override profile chunk sizes (only smaller than the profile hard limit is
    sensible). ``invalid_output=True`` (consumed once) writes a manifest for the wrong story
    version. An already valid adaptation is never rewritten.
    """

    def __init__(self, store, *, results=None, chunking=None, invalid_output=False, runner_type="fake_tts"):
        super().__init__(store, runner_type, results)
        self.chunking = dict(chunking or {})
        self.invalid_output = invalid_output

    def execute(self, packet):
        short = self._scripted(packet)
        if short is not None:
            return short
        i = packet.inputs
        out = i["output_dir"]
        for p in packet.outputs:
            _check_output_path(self.store, p, out)
        _check_output_path(self.store, i["story_artifact"])
        profile = load_profile(i["profile"])
        if validate_tts_artifacts(self.store, out, story_version_id=i["story_version_id"],
                                  profile_id=i["profile"])[0] is None:
            return RunnerResult(code=ResultCode.SUCCESS, metrics={"skipped": True})
        c = profile["chunking"]
        hard = int(c["hard_max_chars"])
        pieces = chunk_text(
            adapt_text(self.store.read(i["story_artifact"]).decode("utf-8")),
            hard=int(self.chunking.get("hard_max_chars", hard)),
            preferred_max=int(self.chunking.get("preferred_chunk_chars_max", c["preferred_chunk_chars_max"])),
            tiny=int(self.chunking.get("avoid_chunk_below_chars", c["avoid_chunk_below_chars"])))
        records = []
        for n, piece in enumerate(pieces, 1):
            name = f"chunks/{n:04d}.txt"
            self.store.write(f"{out}/{name}", (piece + "\n").encode("utf-8"))
            records.append({"index": n, "file": name, "chars": len(piece)})
        adapted = "\n\n".join(pieces) + "\n"
        self.store.write(f"{out}/story_tts.txt", adapted.encode("utf-8"))
        wrong = self.invalid_output
        self.invalid_output = False
        manifest = {
            "profile_id": profile["profile_id"], "source_story": i["story_artifact"],
            "source_story_version_id": "wrong-version" if wrong else i["story_version_id"],
            "adapted_story": f"{out}/story_tts.txt", "total_chars": len(adapted.strip()),
            "chunk_count": len(pieces), "hard_max_chars": hard, "chunks": records,
        }
        self.store.write(f"{out}/manifest.json",  # written last: commit marker
                         (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return RunnerResult(code=ResultCode.SUCCESS, metrics={"chunks": len(pieces)})


class FakeAudioRunner(_ScriptedRunner):
    """Executable contract of the audio job: one wav per chunk, skip-if-valid-exists.

    ``fail_after=k``: (consumed once) after producing k NEW chunks return TASK_FAILED, leaving
    those chunk files in place. ``invalid_output=True`` (consumed once): first produced chunk is
    garbage bytes. Writes only via ArtifactStore.write; a valid chunk is never rewritten.
    """

    def __init__(self, store, *, results=None, fail_after=None, invalid_output=False, runner_type="fake_audio"):
        super().__init__(store, runner_type, results)
        self.fail_after = fail_after
        self.invalid_output = invalid_output
        self.produced: list[str] = []   # paths written across all invocations
        self.skipped: list[str] = []

    def execute(self, packet):
        short = self._scripted(packet)
        if short is not None:
            return short
        out = packet.inputs["output_dir"]
        chunks = packet.inputs["chunks"]
        if len(chunks) != len(packet.outputs):
            raise ValueError("outputs must list one audio file per chunk")
        for p in packet.outputs:
            _check_output_path(self.store, p, out)
        for c in chunks:
            _check_output_path(self.store, c)
        made = 0
        for chunk_path, out_path in zip(chunks, packet.outputs):
            if _valid_audio(self.store, out_path) is not None:
                self.skipped.append(out_path)
                continue
            if self.fail_after is not None and made >= self.fail_after:
                self.fail_after = None
                return RunnerResult(code=ResultCode.TASK_FAILED, error_code="partial_failure",
                                    error_message=f"simulated failure after {made} chunks",
                                    metrics={"produced": made})
            text = self.store.read(chunk_path).decode("utf-8").strip()
            data = fake_wav(text)
            if self.invalid_output:
                self.invalid_output = False
                data = b"not-a-wav"
            self.store.write(out_path, data)
            self.produced.append(out_path)
            made += 1
        return RunnerResult(code=ResultCode.SUCCESS, metrics={"produced": made})


__all__ = [
    "AudioStep", "FakeAudioRunner", "FakeTTSAdapterRunner", "TTSStep", "TTS_VALIDATORS", "UnknownProfile",
    "CRASH", "TIMEOUT", "adapt_text", "chunk_text", "fake_wav", "load_profile", "sync_chunks",
    "validate_tts_artifacts", "wav_duration_ms",
]
