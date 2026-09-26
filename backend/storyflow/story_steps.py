"""Phase 4 workstream 2: source acquisition, canon analysis and story generation steps.

Audit of the pinned skill ``skills/story-branch-writer`` (read only)
====================================================================
The skill is a single-agent, prompt-only workflow (SKILL.md, CLAUDE.md/AGENTS.md, one
workspace scaffold script ``scripts/init_story_project.py``, ``references/state-schema.md``
and ``references/quality-checks.md``). It has no API and no machine-checkable output
contract; its persistent workspace is ``reference.txt, request.md, canon.md, branch.md,
outline.md, story_state.json, story.md``. Its inputs are the reference story plus the
optional ``branch`` / ``direction`` / ``target_length`` (exactly the ``story`` block of
``workflow_config``).

Mapping onto StoryFlow tasks:
* SKILL step 1 "Understand canon"  -> ``canon_analysis`` job (CanonStep). The skill writes
  a free-form ``canon.md``; StoryFlow needs something the pipeline can validate and store,
  so the adapter contract asks for a structured ``canon.json`` (see ``CANON_SCHEMA``).
* SKILL steps 2-10 (choose branch, divergence, causality, outline, state, scene writing,
  QA, final edit) -> ``story_generation`` job (StoryStep). They are internal working memory
  of one agent run; ``branch.md`` / ``outline.md`` / ``story_state.json`` are NOT contract
  outputs. The only deliverable is ``story.md`` ("deliver the finished story only").
* ``init_story_project.py`` is not used: StoryFlow's ArtifactStore replaces the CLI
  workspace, and the script writes absolute paths (``Path.resolve``) which StoryFlow forbids.

StoryFlow-side adapter contract (defined here, executable through the fakes below):
    task packet    skill = {name: story-branch-writer, path: skills/story-branch-writer,
                            revision: <sources.lock.json pin>}
                   task_config.step = "canon" | "story"
    canon inputs   source_artifact (rel path of source text), snapshot_id, project_id
    canon outputs  projects/<pid>/canon/<analysis_id>/canon.json   (CANON_SCHEMA)
    story inputs   source_artifact, canon_artifact (rel path), snapshot_id,
                   canon_analysis_id, project_id, branch, direction, target_length
    story outputs  projects/<pid>/story/<generation_id>/story.md   (UTF-8 prose)
All paths are RELATIVE to the ArtifactStore root; runners write through ArtifactStore.write.

DOCUMENTED GAP: there is NO production AI runner yet. ``gateway.py`` documents that AionUI
exposes no stable task-run API (submit -> id -> poll -> result, cancel), so a real runner
that reads the skill and produces canon.json / story.md cannot be built until it does.
``FakeStoryPipelineRunner`` is the executable contract for now; a real runner must satisfy
the same packet in / artifacts out behaviour and the same validators.

Design rules honoured throughout: short DB transactions, no transaction held during
subtitle fetches or runner work, unique-index guarded inserts that converge concurrent
callers, and no absolute path in any persisted row.
"""

import hashlib
import json
import re
from pathlib import PurePosixPath

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .agents import AgentRunner, CRASH, TIMEOUT, _CrashSentinel
from .artifacts import ArtifactStore, PathTraversalError
from .models import (
    CanonAnalysis,
    DomainStatus,
    PipelineJob,
    SourceSnapshot,
    StoryGeneration,
    StoryProject,
    StoryVersion,
    VersionStatus,
)
from .pipeline import (
    InlineStepHandler,
    JobSpec,
    PipelineContext,
    StepHandler,
    StepStatus,
    StepView,
    workflow_config,
)
from .protocol import ResultCode, RunnerResult, TaskPacket
from .roles import Role
from .subtitles import (
    PROJECT_ROOT,
    BlockedByProvider,
    LanguageUnavailable,
    ProviderTimeout,
    ProviderUnavailable,
    SubtitlesUnavailable,
    plain_text,
)

SKILL_NAME = "story-branch-writer"
SKILL_PATH = "skills/story-branch-writer"

_LIVE = (DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value)
_MAX_TRIES = 5
MIN_STORY_WORDS = 5

# --- canon schema -------------------------------------------------------------

CANON_SCHEMA = {
    "version": 1,                                           # int, must equal 1
    "central_conflict": "str, non-empty",
    "characters": [{"id": "str", "name": "str", "function": "str"}],          # non-empty
    "relationships": [{"from": "character id", "to": "character id", "type": "str"}],  # may be empty
    "events": [{"id": "str", "summary": "str", "causes": ["event ids, optional"]}],    # non-empty
    "leverage_points": [{"id": "str", "description": "str"}],                 # non-empty
}
_CANON_ITEM_KEYS = {
    "characters": ("id", "name", "function"),
    "relationships": ("from", "to", "type"),
    "events": ("id", "summary"),
    "leverage_points": ("id", "description"),
}
_CANON_NONEMPTY = ("characters", "events", "leverage_points")


def validate_canon(obj) -> str | None:
    """None when ``obj`` satisfies CANON_SCHEMA, else a short error message."""
    if not isinstance(obj, dict):
        return "canon must be a JSON object"
    if obj.get("version") != 1:
        return "canon.version must be 1"
    if not isinstance(obj.get("central_conflict"), str) or not obj["central_conflict"].strip():
        return "canon.central_conflict must be a non-empty string"
    for key, fields in _CANON_ITEM_KEYS.items():
        items = obj.get(key)
        if not isinstance(items, list):
            return f"canon.{key} must be a list"
        if key in _CANON_NONEMPTY and not items:
            return f"canon.{key} must not be empty"
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                return f"canon.{key}[{i}] must be an object"
            for f in fields:
                if not isinstance(item.get(f), str) or not item[f].strip():
                    return f"canon.{key}[{i}].{f} must be a non-empty string"
    char_ids = {c["id"] for c in obj["characters"]}
    for i, rel in enumerate(obj["relationships"]):
        if rel["from"] not in char_ids or rel["to"] not in char_ids:
            return f"canon.relationships[{i}] references an unknown character"
    return None


_ABS_PATH_RE = re.compile(r"\b[A-Za-z]:[\\/]|(?:^|[\s\"'(])/(?:home|Users|tmp|var|etc|usr|mnt|root|opt)/")


MIN_LENGTH_RATIO = 0.85  # a story shorter than this share of target_length is rejected (and rewritten)


def check_story_text(text: str, store_root: str | None = None, target_length=None) -> str | None:
    """None when story prose is acceptable, else an error message."""
    if not text.strip():
        return "story is empty"
    words = len(text.split())
    if words < MIN_STORY_WORDS:
        return f"story has fewer than {MIN_STORY_WORDS} words"
    if isinstance(target_length, int) and not isinstance(target_length, bool) and target_length > 0:
        if words < target_length * MIN_LENGTH_RATIO:
            return f"story is too short ({words} words, at least {int(target_length * MIN_LENGTH_RATIO)} required)"
    if "\x00" in text:
        return "story contains NUL bytes"
    if _ABS_PATH_RE.search(text) or (store_root and store_root in text):
        return "story contains an absolute path"
    return None


# --- helpers ------------------------------------------------------------------


def skill_pin() -> dict:
    """Skill descriptor for job payloads; revision comes from sources.lock.json."""
    lock = json.loads((PROJECT_ROOT / "sources.lock.json").read_text(encoding="utf-8"))
    revision = lock["skills"]["projects"][SKILL_NAME]["revision"]
    return {"name": SKILL_NAME, "path": SKILL_PATH, "revision": revision}


def source_path(project_id: str, number: int) -> str:
    return f"projects/{project_id}/source/{number:04d}/source.txt"


def canon_path(project_id: str, analysis_id: str) -> str:
    return f"projects/{project_id}/canon/{analysis_id}/canon.json"


def story_path(project_id: str, generation_id: str) -> str:
    return f"projects/{project_id}/story/{generation_id}/story.md"


def _unique_violation(exc: IntegrityError, *needles: str) -> bool:
    msg = str(exc.orig)
    return "UNIQUE constraint failed" in msg and all(n in msg for n in needles)


def _get(db, model, pk):
    return db.get(model, pk, populate_existing=True)


def _active_snapshot(db, project_id: str) -> SourceSnapshot | None:
    return db.scalars(
        select(SourceSnapshot)
        .where(SourceSnapshot.story_project_id == project_id,
               SourceSnapshot.status == VersionStatus.ACTIVE.value)
        .order_by(SourceSnapshot.snapshot_number.desc()).limit(1),
        execution_options={"populate_existing": True},
    ).first()


def _next_snapshot_number(db, project_id: str) -> int:
    top = db.scalar(select(func.max(SourceSnapshot.snapshot_number))
                    .where(SourceSnapshot.story_project_id == project_id))
    return (top or 0) + 1


def _next_version_number(db, project_id: str) -> int:
    top = db.scalar(select(func.max(StoryVersion.version_number))
                    .where(StoryVersion.story_project_id == project_id))
    return (top or 0) + 1


class OutputPathError(ValueError):
    """A packet output path is not a safe artifact path for this task."""


def safe_output_path(packet: TaskPacket, store: ArtifactStore, filename: str) -> str:
    """The single output path of a canon/story packet, validated to be relative, traversal
    safe (store.resolve) and located under projects/<project_id>/."""
    outputs = packet.outputs or []
    if len(outputs) != 1 or not isinstance(outputs[0], str):
        raise OutputPathError("packet must list exactly one output path")
    rel = outputs[0]
    pid = (packet.inputs or {}).get("project_id")
    if not pid:
        raise OutputPathError("packet.inputs.project_id missing")
    try:
        store.resolve(rel)
    except PathTraversalError as exc:
        raise OutputPathError(f"unsafe output path: {rel}") from exc
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] != "projects" or parts[1] != pid or parts[-1] != filename:
        raise OutputPathError(f"output path must be projects/{pid}/.../{filename}")
    return rel


# --- source step --------------------------------------------------------------


class SourceStep(InlineStepHandler):
    """Fetch the source transcript through ctx.subtitle_client and freeze it as a snapshot.

    Provider error mapping (documented choice):
      SubtitlesUnavailable -> FAILED   error_code "subtitles_unavailable" (permanent)
      LanguageUnavailable  -> FAILED   error_code "language_unavailable"  (permanent for this config)
      BlockedByProvider    -> NOT_STARTED error_code "provider_blocked"   (transient: nothing
                              was persisted, so no domain row exists; the next tick retries.
                              Not FAILED, because a block is not a business failure.)
      ProviderTimeout      -> NOT_STARTED error_code "provider_timeout"   (transient, like a block)
      ProviderUnavailable  -> FAILED   error_code "provider_unavailable"  (operator must fix
                              the provider install/config; resume/retry re-runs the step)
    Snapshots are immutable: an existing active snapshot is returned as-is, never replaced.
    """

    step = "source"

    def status(self, db, ctx, project):
        snap = _active_snapshot(db, project.id)
        if snap is None:
            return StepView(StepStatus.NOT_STARTED)
        return StepView(StepStatus.COMPLETED, domain_id=snap.id)

    def run(self, ctx: PipelineContext, project_id: str) -> StepView:
        with ctx.session_factory() as db:
            project = _get(db, StoryProject, project_id)
            if project is None:
                return StepView(StepStatus.FAILED, error_code="project_not_found")
            existing = _active_snapshot(db, project_id)
            if existing is not None:
                return StepView(StepStatus.COMPLETED, domain_id=existing.id)
            cfg = dict(workflow_config(db, project).get("source") or {})
            title = project.title
        video_id = cfg.get("video_id")
        if not video_id:
            return StepView(StepStatus.FAILED, error_code="source_not_configured")

        try:  # no DB transaction is open here
            fetched = ctx.subtitle_client.fetch(
                video_id, cfg.get("languages"), cfg.get("preference", "any"),
                cfg.get("allow_translation", True))
        except SubtitlesUnavailable:
            return StepView(StepStatus.FAILED, error_code="subtitles_unavailable")
        except LanguageUnavailable:
            return StepView(StepStatus.FAILED, error_code="language_unavailable")
        except (BlockedByProvider, ProviderTimeout) as exc:
            code = "provider_timeout" if isinstance(exc, ProviderTimeout) else "provider_blocked"
            return StepView(StepStatus.NOT_STARTED, error_code=code)
        except ProviderUnavailable:
            return StepView(StepStatus.FAILED, error_code="provider_unavailable")

        content = plain_text(fetched)
        if not content.strip():
            return StepView(StepStatus.FAILED, error_code="empty_source")
        data = content.encode("utf-8")
        content_hash = hashlib.sha256(data).hexdigest()

        floor = 0
        for _ in range(_MAX_TRIES):
            with ctx.session_factory() as db:
                existing = _active_snapshot(db, project_id)
                if existing is not None:
                    return StepView(StepStatus.COMPLETED, domain_id=existing.id)
                number = max(_next_snapshot_number(db, project_id), floor)
            rel = source_path(project_id, number)
            if ctx.store.exists(rel):
                if ctx.store.read(rel) != data:  # orphan/racing artifact of other content: never overwrite
                    floor = number + 1
                    continue
            else:
                ctx.store.write(rel, data)
            meta = {
                "video_id": video_id, "language": fetched.language,
                "language_code": fetched.language_code, "is_generated": fetched.is_generated,
                "translated": fetched.translated,
                "provider": type(ctx.subtitle_client).__name__, "artifact_path": rel,
            }
            snap = self._insert_snapshot(ctx, project_id, number, title, content, content_hash, meta)
            if snap is not None:
                return StepView(StepStatus.COMPLETED, domain_id=snap)
        return StepView(StepStatus.IN_PROGRESS, error_code="snapshot_contention")

    @staticmethod
    def _insert_snapshot(ctx, project_id, number, title, content, content_hash, meta) -> str | None:
        """Insert one active snapshot; None if the unique (project, number) index was hit."""
        with ctx.session_factory() as db:
            row = SourceSnapshot(story_project_id=project_id, snapshot_number=number, title=title,
                                 content=content, content_hash=content_hash, meta=meta,
                                 status=VersionStatus.ACTIVE.value)
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "source_snapshots.snapshot_number"):
                    raise
                return None
            return row.id


# --- shared job-backed handler plumbing ---------------------------------------


class _JobStep(StepHandler):
    model = None  # domain model class

    def _link(self, db, domain_id, job):
        db.execute(update(self.model).where(self.model.id == domain_id,
                                            self.model.status.in_(_LIVE))
                   .values(pipeline_job_id=job.id))
        db.commit()

    def link_job(self, db, ctx, domain_id, job):
        self._link(db, domain_id, job)

    def _fail(self, db, ctx, domain_id, code, message):
        now = ctx.clock()
        db.execute(update(self.model).where(self.model.id == domain_id, self.model.status.in_(_LIVE))
                   .values(status=DomainStatus.FAILED.value, error_code=code,
                           error_message=(message or "")[:400], updated_at=now, finished_at=now))
        db.commit()

    def mark_failed(self, db, ctx, domain_id, job):
        self._fail(db, ctx, domain_id, job.last_error_code or "job_failed", job.last_error_message)

    @staticmethod
    def _pick(rows):
        """completed wins; else newest live; else newest failed -> FAILED; else NOT_STARTED."""
        rows = sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)
        for r in rows:
            if r.status == DomainStatus.COMPLETED.value:
                return StepView(StepStatus.COMPLETED, domain_id=r.id, pipeline_job_id=r.pipeline_job_id)
        for r in rows:
            if r.status in _LIVE:
                return StepView(StepStatus.IN_PROGRESS, domain_id=r.id, pipeline_job_id=r.pipeline_job_id)
        for r in rows:
            if r.status == DomainStatus.FAILED.value:
                return StepView(StepStatus.FAILED, domain_id=r.id, pipeline_job_id=r.pipeline_job_id,
                                error_code=r.error_code)
        return StepView(StepStatus.NOT_STARTED)


# --- canon step ---------------------------------------------------------------


class CanonStep(_JobStep):
    """canon_analysis job: source snapshot -> CanonAnalysis.canon (validated CANON_SCHEMA)."""

    step = "canon"
    job_kind = "canon_analysis"
    role = Role.STORY_WRITER.value
    model = CanonAnalysis

    def _rows(self, db, snapshot_id):
        return db.scalars(select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snapshot_id),
                          execution_options={"populate_existing": True}).all()

    def status(self, db, ctx, project):
        snap = _active_snapshot(db, project.id)
        if snap is None:
            return StepView(StepStatus.NOT_STARTED)
        return self._pick(self._rows(db, snap.id))

    def begin(self, db, ctx, project):
        """None when there is no snapshot or a completed analysis already exists (nothing to
        begin). A failed analysis never blocks: begin creates a fresh one."""
        snap = _active_snapshot(db, project.id)
        artifact = (snap.meta or {}).get("artifact_path") if snap else None
        if snap is None or not artifact:
            return None
        analysis = None
        for _ in range(2):
            rows = self._rows(db, snap.id)
            if any(r.status == DomainStatus.COMPLETED.value for r in rows):
                return None
            live = [r for r in rows if r.status in _LIVE]
            if live:
                analysis = live[0]
                break
            candidate = CanonAnalysis(source_snapshot_id=snap.id, status=DomainStatus.QUEUED.value,
                                      created_at=ctx.clock(), updated_at=ctx.clock())
            db.add(candidate)
            try:
                db.commit()
                analysis = candidate
                break
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "canon_analyses.source_snapshot_id"):
                    raise
        if analysis is None:
            return None
        spec = JobSpec(
            kind=self.job_kind, role=self.role, dedupe_key=f"canon:{analysis.id}",
            payload={
                "skill": skill_pin(),
                "inputs": {"source_artifact": artifact, "snapshot_id": snap.id, "project_id": project.id},
                "outputs": [canon_path(project.id, analysis.id)],
                "task_config": {"step": "canon", "schema_version": 1, "analysis_id": analysis.id},
            })
        return analysis.id, spec

    def finalize(self, db, ctx, domain_id, job):
        analysis = _get(db, CanonAnalysis, domain_id)
        if analysis is None or analysis.status not in _LIVE:
            return
        snap = _get(db, SourceSnapshot, analysis.source_snapshot_id)
        try:
            raw = ctx.store.read(canon_path(snap.story_project_id, analysis.id))
        except FileNotFoundError:
            return self._fail(db, ctx, domain_id, "missing_output", "canon.json not found")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._fail(db, ctx, domain_id, "invalid_canon", "canon.json is not valid UTF-8 JSON")
        problem = validate_canon(obj)
        if problem:
            return self._fail(db, ctx, domain_id, "invalid_canon", problem)
        now = ctx.clock()
        db.execute(update(CanonAnalysis).where(CanonAnalysis.id == domain_id, CanonAnalysis.status.in_(_LIVE))
                   .values(status=DomainStatus.COMPLETED.value, canon=obj, error_code=None,
                           error_message=None, updated_at=now, finished_at=now))
        db.commit()


# --- story step ---------------------------------------------------------------


class StoryStep(_JobStep):
    """story_generation job: completed canon -> StoryGeneration -> immutable StoryVersion."""

    step = "story"
    job_kind = "story_generation"
    role = Role.STORY_WRITER.value
    model = StoryGeneration

    def _rows(self, db, snapshot_id):
        return db.scalars(select(StoryGeneration).where(StoryGeneration.source_snapshot_id == snapshot_id),
                          execution_options={"populate_existing": True}).all()

    def status(self, db, ctx, project):
        snap = _active_snapshot(db, project.id)
        if snap is None:
            return StepView(StepStatus.NOT_STARTED)
        return self._pick(self._rows(db, snap.id))

    def begin(self, db, ctx, project):
        """None without a completed canon, or when a completed generation exists for this
        input (the live unique index forbids a second one). Failed generations never block."""
        snap = _active_snapshot(db, project.id)
        if snap is None or not (snap.meta or {}).get("artifact_path"):
            return None
        canon = db.scalars(
            select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snap.id,
                                        CanonAnalysis.status == DomainStatus.COMPLETED.value)
            .order_by(CanonAnalysis.created_at.desc()).limit(1),
            execution_options={"populate_existing": True}).first()
        if canon is None:
            return None
        cfg = dict(workflow_config(db, project).get("story") or {})
        story_cfg = {k: cfg.get(k) for k in ("branch", "direction", "target_length")}
        if not story_cfg["target_length"]:  # default: at least as long as the reference story
            try:
                story_cfg["target_length"] = len(ctx.store.read(snap.meta["artifact_path"]).decode("utf-8").split())
            except (OSError, UnicodeDecodeError):
                pass
        gen = None
        for _ in range(2):
            rows = [r for r in self._rows(db, snap.id) if r.canon_analysis_id == canon.id]
            if any(r.status == DomainStatus.COMPLETED.value for r in rows):
                return None
            live = [r for r in rows if r.status in _LIVE]
            if live:
                gen = live[0]
                break
            candidate = StoryGeneration(story_project_id=project.id, source_snapshot_id=snap.id,
                                        canon_analysis_id=canon.id, trigger="pipeline",
                                        status=DomainStatus.QUEUED.value, config=story_cfg,
                                        created_at=ctx.clock(), updated_at=ctx.clock())
            db.add(candidate)
            try:
                db.commit()
                gen = candidate
                break
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "story_generations.story_project_id"):
                    raise
        if gen is None:
            return None
        cfg_used = dict(gen.config or story_cfg)
        spec = JobSpec(
            kind=self.job_kind, role=self.role, dedupe_key=f"story:{gen.id}",
            payload={
                "skill": skill_pin(),
                "inputs": {"source_artifact": snap.meta["artifact_path"],
                           "canon_artifact": canon_path(project.id, canon.id),
                           "snapshot_id": snap.id, "canon_analysis_id": canon.id,
                           "project_id": project.id, **cfg_used},
                "outputs": [story_path(project.id, gen.id)],
                "task_config": {"step": "story", "generation_id": gen.id, **cfg_used},
            })
        return gen.id, spec

    def finalize(self, db, ctx, domain_id, job):
        gen = _get(db, StoryGeneration, domain_id)
        if gen is None:
            return
        for _ in range(_MAX_TRIES):
            version = db.scalar(select(StoryVersion.id).where(StoryVersion.story_generation_id == gen.id))
            if version is not None:
                # Already materialised: only make sure the generation is marked completed.
                now = ctx.clock()
                db.execute(update(StoryGeneration).where(StoryGeneration.id == gen.id,
                                                         StoryGeneration.status.in_(_LIVE))
                           .values(status=DomainStatus.COMPLETED.value, updated_at=now, finished_at=now))
                db.commit()
                return
            if gen.status not in _LIVE:
                return
            rel = story_path(gen.story_project_id, gen.id)
            try:
                raw = ctx.store.read(rel)
            except FileNotFoundError:
                return self._fail(db, ctx, domain_id, "missing_output", "story.md not found")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return self._fail(db, ctx, domain_id, "invalid_story", "story.md is not valid UTF-8")
            problem = check_story_text(text, str(ctx.store.root), (gen.config or {}).get("target_length"))
            if problem:
                return self._fail(db, ctx, domain_id, "invalid_story", problem)
            project = _get(db, StoryProject, gen.story_project_id)
            number = _next_version_number(db, gen.story_project_id)
            now = ctx.clock()
            claimed = db.execute(
                update(StoryGeneration).where(StoryGeneration.id == gen.id, StoryGeneration.status.in_(_LIVE))
                .values(status=DomainStatus.COMPLETED.value, error_code=None, error_message=None,
                        updated_at=now, finished_at=now))
            if claimed.rowcount != 1:
                db.rollback()
                return
            db.add(StoryVersion(story_generation_id=gen.id, story_project_id=gen.story_project_id,
                                version_number=number, title=project.title, content=text,
                                word_count=len(text.split()), content_path=rel,
                                status=VersionStatus.ACTIVE.value, created_at=now, updated_at=now))
            try:
                db.commit()  # generation completion + version are one transaction
                return
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "story_versions.story_project_id"):
                    raise
                gen = _get(db, StoryGeneration, domain_id)  # lost the number race: re-evaluate
        # contention exhausted: leave the generation live; the next tick finalizes again.


# --- validators (turn invalid runner output into ResultCode.INVALID_OUTPUT) ---


def validate_canon_output(packet: TaskPacket, store: ArtifactStore) -> str | None:
    try:
        rel = safe_output_path(packet, store, "canon.json")
        raw = store.read(rel)
    except OutputPathError as exc:
        return str(exc)
    except FileNotFoundError:
        return "canon.json was not produced"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "canon.json is not valid UTF-8 JSON"
    return validate_canon(obj)


def validate_story_output(packet: TaskPacket, store: ArtifactStore) -> str | None:
    try:
        rel = safe_output_path(packet, store, "story.md")
        raw = store.read(rel)
    except OutputPathError as exc:
        return str(exc)
    except FileNotFoundError:
        return "story.md was not produced"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "story.md is not valid UTF-8"
    return check_story_text(text, str(store.root), (packet.task_config or {}).get("target_length"))


STORY_VALIDATORS = {"canon": validate_canon_output, "story": validate_story_output}


# --- deterministic fake runner --------------------------------------------------


class FakeStoryPipelineRunner(AgentRunner):
    """Executable contract for the canon/story adapter (no production AI runner exists yet).

    Reads the packet's input artifacts from the store and writes deterministic outputs to
    ``packet.outputs`` through ArtifactStore.write. ``results`` is a queue consumed in order
    (ResultCode / RunnerResult / CRASH / TIMEOUT); an empty queue means SUCCESS. A non-success
    entry writes nothing. ``emit_invalid`` (True or a set of steps such as {"story"}) writes
    malformed output while still returning SUCCESS, to be caught by OutputValidatingRunner.
    """

    runner_type = "fake_story"

    def __init__(self, store: ArtifactStore, *, results=None, emit_invalid=False, roles=None,
                 runner_type="fake_story"):
        self.store = store
        self.runner_type = runner_type
        self._pending = list(results or [])
        self.emit_invalid = emit_invalid
        self.roles = set(roles) if roles is not None else {Role.STORY_WRITER.value}
        self.invocations: list[TaskPacket] = []

    def _invalid(self, step: str) -> bool:
        flag = self.emit_invalid
        return bool(flag) if isinstance(flag, bool) else step in flag

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED

    def execute(self, packet: TaskPacket) -> RunnerResult:
        self.invocations.append(packet)
        item = self._pending.pop(0) if self._pending else ResultCode.SUCCESS
        if isinstance(item, _CrashSentinel):
            raise item.exc
        if item is TIMEOUT:
            raise item
        if isinstance(item, RunnerResult):
            if item.code is not ResultCode.SUCCESS:
                return item
        elif item is not ResultCode.SUCCESS:
            return RunnerResult(code=item, metrics={"invocations": len(self.invocations)})

        step = (packet.task_config or {}).get("step")
        if step not in ("canon", "story"):
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="unknown_step",
                                error_message=f"unsupported step {step!r}")
        filename = "canon.json" if step == "canon" else "story.md"
        try:
            rel = safe_output_path(packet, self.store, filename)
            if step == "canon":
                data = self._canon(packet)
            else:
                data = self._story(packet)
        except OutputPathError as exc:
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="bad_output_path",
                                error_message=str(exc))
        except (FileNotFoundError, KeyError):
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="missing_input",
                                error_message="input artifact not found")
        self.store.write(rel, data)
        return RunnerResult(code=ResultCode.SUCCESS, artifacts={"outputs": [rel]},
                            metrics={"invocations": len(self.invocations)})

    def _canon(self, packet: TaskPacket) -> bytes:
        source = self.store.read(packet.inputs["source_artifact"]).decode("utf-8")
        if self._invalid("canon"):
            return b"{ this is not json"
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
        first_line = (source.strip().splitlines() or ["untitled"])[0][:80]
        canon = {
            "version": 1,
            "central_conflict": f"conflict derived from source {digest}",
            "characters": [
                {"id": "c1", "name": "Protagonist", "function": "protagonist"},
                {"id": "c2", "name": "Antagonist", "function": "antagonist"},
            ],
            "relationships": [{"from": "c1", "to": "c2", "type": "rivalry"}],
            "events": [
                {"id": "e1", "summary": f"opening: {first_line}"},
                {"id": "e2", "summary": "escalation", "causes": ["e1"]},
            ],
            "leverage_points": [{"id": "l1", "description": "the protagonist's first decision"}],
        }
        return json.dumps(canon, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    def _story(self, packet: TaskPacket) -> bytes:
        canon = json.loads(self.store.read(packet.inputs["canon_artifact"]).decode("utf-8"))
        if self._invalid("story"):
            return b""
        branch = (packet.inputs or {}).get("branch") or "an automatically chosen branch"
        name = canon["characters"][0]["name"]
        text = (
            f"# Story\n\n{name} steps into {branch}. "
            f"{canon['central_conflict']}. Every choice now carries a consequence, "
            "and the rival answers each move. In the end the truth comes out.\n"
        )
        target = (packet.inputs or {}).get("target_length")
        if isinstance(target, int) and not isinstance(target, bool) and target > 0:
            # honour the length contract like a real writer would: keep developing scenes
            n = 0
            while len(text.split()) < target:
                n += 1
                text += f"\nScene {n}: {name} weighs another consequence while the rival answers in kind.\n"
        return text.encode("utf-8")


FakeCanonRunner = FakeStoryRunner = FakeStoryPipelineRunner
