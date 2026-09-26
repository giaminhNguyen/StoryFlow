"""Story review + revision (roadmap 4.4): the ``review`` step between ``story`` and ``tts``.

One ``story_review`` job (role story_writer) reads the source transcript, the canon and the LATEST story
version and returns a verdict. When the workflow's ``review.revise`` is on and the verdict is "revise", the same
call also returns the corrected story, which becomes the newest StoryVersion (the older one stays ACTIVE: the
highest ``version_number`` wins, exactly what TTSStep and the read models already pick). Up to
``review.max_rounds`` rounds: after a revision the NEW version is reviewed again; when the rounds are used up the
last revision is accepted unreviewed.

Presets decide whether the step exists (``ReviewStep.enabled``); under ``fast`` it is invisible to the
orchestrator and the read models. Contract (shared with the real runner in ``integrations/claude_cli.py``)::

    job kind story_review, role story_writer, dedupe key ``review:<review_id>``
    inputs   {"project_id", "source_artifact", "canon_artifact", "story_artifact", "story_version_id",
              "review_id", "round_number", "revise": bool, "target_length": int | None}
    outputs  ["projects/<pid>/review/<review_id>/review.json"]
             + ["projects/<pid>/review/<review_id>/story_revised.md"]   (only when revise)
    review.json = {"version": 1, "verdict": "approve" | "revise", "summary": str,
                   "issues": [{"aspect": "canon|logic|style|length|other", "severity": "low|medium|high",
                               "note": str}]}

Output problems (bad JSON, unknown verdict, a revision that is missing or shorter than 85% of the reviewed story)
are caught INSIDE the dispatcher path (``validate_review_output``): they become business failures with the usual
bounded retry, so ``finalize`` only ever sees valid output.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .agents import AgentRunner, CRASH, TIMEOUT, _CrashSentinel
from .artifacts import ArtifactStore, PathTraversalError
from .models import CanonAnalysis, DomainStatus, StoryReview, StoryVersion, VersionStatus, uid
from .pipeline import JobSpec, StepStatus, StepView, workflow_config
from .presets import review_from_config
from .protocol import ResultCode, RunnerResult, TaskPacket
from .roles import Role
from .story_steps import (
    OutputPathError,
    _JobStep,
    _LIVE,
    _MAX_TRIES,
    _active_snapshot,
    _get,
    _next_version_number,
    _unique_violation,
    canon_path,
    check_story_text,
    skill_pin,
)
STEP = "review"
JOB_KIND = "story_review"

ASPECTS = ("canon", "logic", "style", "length", "other")
SEVERITIES = ("low", "medium", "high")
VERDICTS = ("approve", "revise")
MAX_SUMMARY_CHARS = 2000
MAX_ISSUES = 50
MAX_NOTE_CHARS = 1000


def review_path(project_id: str, review_id: str) -> str:
    return f"projects/{project_id}/review/{review_id}/review.json"


def revised_story_path(project_id: str, review_id: str) -> str:
    return f"projects/{project_id}/review/{review_id}/story_revised.md"


# --- review.json schema ---------------------------------------------------------------------


def validate_review(obj) -> str | None:
    """None when ``review.json`` content is acceptable, else a short error message."""
    if not isinstance(obj, dict):
        return "review must be a JSON object"
    if obj.get("version") != 1 or isinstance(obj.get("version"), bool):
        return "review.version must be 1"
    if obj.get("verdict") not in VERDICTS:
        return "review.verdict must be 'approve' or 'revise'"
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "review.summary must be a non-empty string"
    if len(summary) > MAX_SUMMARY_CHARS:
        return f"review.summary is longer than {MAX_SUMMARY_CHARS} characters"
    issues = obj.get("issues")
    if not isinstance(issues, list):
        return "review.issues must be a list"
    if len(issues) > MAX_ISSUES:
        return f"review.issues has more than {MAX_ISSUES} entries"
    for i, issue in enumerate(issues):
        if not isinstance(issue, dict):
            return f"review.issues[{i}] must be an object"
        if issue.get("aspect") not in ASPECTS:
            return f"review.issues[{i}].aspect must be one of {', '.join(ASPECTS)}"
        if issue.get("severity") not in SEVERITIES:
            return f"review.issues[{i}].severity must be one of {', '.join(SEVERITIES)}"
        note = issue.get("note")
        if not isinstance(note, str) or not note.strip():
            return f"review.issues[{i}].note must be a non-empty string"
        if len(note) > MAX_NOTE_CHARS:
            return f"review.issues[{i}].note is longer than {MAX_NOTE_CHARS} characters"
    return None


def _review_outputs(packet: TaskPacket, store: ArtifactStore) -> tuple[str, str | None]:
    """(review.json path, story_revised.md path or None) of a review packet, validated to be relative,
    traversal safe and located under projects/<project_id>/review/<review_id>/."""
    outputs = packet.outputs or []
    revise = bool((packet.task_config or {}).get("revise"))
    if len(outputs) != (2 if revise else 1) or not all(isinstance(o, str) for o in outputs):
        raise OutputPathError("packet must list review.json" + (" and story_revised.md" if revise else "") +
                              " as its outputs")
    pid = (packet.inputs or {}).get("project_id")
    if not pid:
        raise OutputPathError("packet.inputs.project_id missing")
    review_id = (packet.task_config or {}).get("review_id") or (packet.inputs or {}).get("review_id")
    if not review_id:
        raise OutputPathError("packet review_id missing")
    for rel, filename in zip(outputs, ("review.json", "story_revised.md")):
        try:
            store.resolve(rel)
        except PathTraversalError as exc:
            raise OutputPathError(f"unsafe output path: {rel}") from exc
        parts = PurePosixPath(rel.replace("\\", "/")).parts
        if parts != ("projects", pid, "review", review_id, filename):
            raise OutputPathError(f"output path must be projects/{pid}/review/{review_id}/{filename}")
    return outputs[0], (outputs[1] if revise else None)


def validate_review_output(packet: TaskPacket, store: ArtifactStore) -> str | None:
    try:
        review_rel, revised_rel = _review_outputs(packet, store)
        raw = store.read(review_rel)
    except OutputPathError as exc:
        return str(exc)
    except FileNotFoundError:
        return "review.json was not produced"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "review.json is not valid UTF-8 JSON"
    problem = validate_review(obj)
    if problem:
        return problem
    if obj["verdict"] == "revise" and revised_rel is not None:
        try:
            text = store.read(revised_rel).decode("utf-8")
        except FileNotFoundError:
            return "story_revised.md was not produced"
        except UnicodeDecodeError:
            return "story_revised.md is not valid UTF-8"
        target = (packet.task_config or {}).get("target_length")
        return check_story_text(text, str(store.root), target)
    return None   # approve: a stray story_revised.md is ignored (never read, never registered)


REVIEW_VALIDATORS = {STEP: validate_review_output}


# --- the step -------------------------------------------------------------------------------


def _latest_version(db, project_id: str) -> StoryVersion | None:
    return db.scalar(
        select(StoryVersion)
        .where(StoryVersion.story_project_id == project_id, StoryVersion.status == VersionStatus.ACTIVE.value)
        .order_by(StoryVersion.version_number.desc()).limit(1),
        execution_options={"populate_existing": True})


class ReviewStep(_JobStep):
    """story_review job: latest StoryVersion -> StoryReview (verdict, issues) [-> a newer StoryVersion]."""

    step = STEP
    job_kind = JOB_KIND
    role = Role.STORY_WRITER.value
    model = StoryReview

    def _reviews(self, db, project_id: str) -> list[StoryReview]:
        return list(db.scalars(select(StoryReview).where(StoryReview.story_project_id == project_id),
                               execution_options={"populate_existing": True}).all())

    def enabled(self, db, ctx, project) -> bool:
        return review_from_config(workflow_config(db, project)).enabled

    def status(self, db, ctx, project):
        version = _latest_version(db, project.id)
        if version is None:
            return StepView(StepStatus.NOT_STARTED)
        rows = self._reviews(db, project.id)
        view = self._pick([r for r in rows if r.story_version_id == version.id])
        if view.status is not StepStatus.NOT_STARTED:
            return view
        completed = sum(1 for r in rows if r.status == DomainStatus.COMPLETED.value)
        if completed >= review_from_config(workflow_config(db, project)).max_rounds:
            return StepView(StepStatus.COMPLETED)   # rounds used up: the last revision is accepted unreviewed
        return StepView(StepStatus.NOT_STARTED)

    def begin(self, db, ctx, project):
        """None without a story version, when this version is already reviewed, or when the rounds are used
        up. A failed review never blocks: begin creates a fresh one."""
        version = _latest_version(db, project.id)
        if version is None or not version.content_path:
            return None
        settings = review_from_config(workflow_config(db, project))
        snap = _active_snapshot(db, project.id)
        source = (snap.meta or {}).get("artifact_path") if snap else None
        canon = db.scalars(
            select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snap.id,
                                        CanonAnalysis.status == DomainStatus.COMPLETED.value)
            .order_by(CanonAnalysis.created_at.desc()).limit(1),
            execution_options={"populate_existing": True}).first() if snap is not None else None
        if not source or canon is None:
            return None
        review = None
        for _ in range(2):
            rows = self._reviews(db, project.id)
            completed = [r for r in rows if r.status == DomainStatus.COMPLETED.value]
            if len(completed) >= settings.max_rounds or any(r.story_version_id == version.id for r in completed):
                return None
            live = [r for r in rows if r.story_version_id == version.id and r.status in _LIVE]
            if live:
                review = live[0]
                break
            now = ctx.clock()
            candidate = StoryReview(
                story_project_id=project.id, story_version_id=version.id, round_number=len(completed) + 1,
                status=DomainStatus.QUEUED.value,
                config={"revise": settings.revise, "max_rounds": settings.max_rounds,
                        "target_length": version.word_count or len((version.content or "").split()) or None},
                created_at=now, updated_at=now)
            db.add(candidate)
            try:
                db.commit()
                review = candidate
                break
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "story_reviews.story_version_id"):
                    raise
        if review is None:
            return None
        cfg = dict(review.config or {})
        revise = bool(cfg.get("revise"))
        outputs = [review_path(project.id, review.id)] + ([revised_story_path(project.id, review.id)] if revise else [])
        spec = JobSpec(
            kind=self.job_kind, role=self.role, dedupe_key=f"review:{review.id}",
            payload={
                "skill": skill_pin(),
                "inputs": {"project_id": project.id, "source_artifact": source,
                           "canon_artifact": canon_path(project.id, canon.id),
                           "story_artifact": version.content_path, "story_version_id": version.id,
                           "review_id": review.id, "round_number": review.round_number, "revise": revise,
                           "target_length": cfg.get("target_length")},
                "outputs": outputs,
                "task_config": {"step": STEP, "review_id": review.id, "revise": revise,
                                "target_length": cfg.get("target_length")},
            })
        return review.id, spec

    def finalize(self, db, ctx, domain_id, job):
        review = _get(db, StoryReview, domain_id)
        if review is None or review.status not in _LIVE:
            return   # unknown, already completed (idempotent) or failed: nothing to do
        cfg = dict(review.config or {})
        rel = review_path(review.story_project_id, review.id)
        try:
            raw = ctx.store.read(rel)
        except FileNotFoundError:
            return self._fail(db, ctx, domain_id, "missing_output", "review.json not found")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._fail(db, ctx, domain_id, "invalid_review", "review.json is not valid UTF-8 JSON")
        problem = validate_review(obj)
        if problem:
            return self._fail(db, ctx, domain_id, "invalid_review", problem)
        revise = bool(cfg.get("revise")) and obj["verdict"] == "revise"
        text = None
        revised_rel = revised_story_path(review.story_project_id, review.id)
        if revise:
            try:
                text = ctx.store.read(revised_rel).decode("utf-8")
            except FileNotFoundError:
                return self._fail(db, ctx, domain_id, "missing_output", "story_revised.md not found")
            except UnicodeDecodeError:
                return self._fail(db, ctx, domain_id, "invalid_story", "story_revised.md is not valid UTF-8")
            problem = check_story_text(text, str(ctx.store.root), cfg.get("target_length"))
            if problem:
                return self._fail(db, ctx, domain_id, "invalid_story", problem)
        for _ in range(_MAX_TRIES):
            now = ctx.clock()
            values = dict(status=DomainStatus.COMPLETED.value, verdict=obj["verdict"], summary=obj["summary"],
                          findings=list(obj["issues"]), error_code=None, error_message=None,
                          updated_at=now, finished_at=now)
            new_id = None
            if revise:
                new_id = uid()
                values["revised_version_id"] = new_id
            claimed = db.execute(update(StoryReview)
                                 .where(StoryReview.id == review.id, StoryReview.status.in_(_LIVE)).values(**values))
            if claimed.rowcount != 1:
                db.rollback()
                return
            if revise:
                old = db.get(StoryVersion, review.story_version_id)
                db.add(StoryVersion(
                    id=new_id, story_generation_id=old.story_generation_id if old else None,
                    story_project_id=review.story_project_id,
                    version_number=_next_version_number(db, review.story_project_id),
                    title=old.title if old else "", content=text, word_count=len(text.split()),
                    content_path=revised_rel, status=VersionStatus.ACTIVE.value, created_at=now, updated_at=now))
            try:
                db.commit()   # review completion + the new version are one transaction
                return
            except IntegrityError as exc:
                db.rollback()
                if not _unique_violation(exc, "story_versions.story_project_id"):
                    raise
                review = _get(db, StoryReview, domain_id)   # lost the version-number race: re-evaluate
                if review is None or review.status not in _LIVE:
                    return
        # contention exhausted: leave the review live; the next tick finalizes again.


# --- deterministic fake runner --------------------------------------------------------------


class FakeReviewRunner(AgentRunner):
    """Executable contract for the review adapter (the real one lives in integrations/claude_cli.py).

    Reads ``story_artifact`` from the store and writes ``review.json`` (+ ``story_revised.md`` when the packet
    asks for a revision and the verdict is "revise") through ArtifactStore.write. ``verdicts`` is consumed one per
    call ("approve" | "revise"; empty = approve). ``results`` is a queue of scripted outcomes consumed in order
    (ResultCode / RunnerResult / CRASH / TIMEOUT); a non-success entry writes nothing. ``emit_invalid`` writes
    malformed output while still returning SUCCESS, to be caught by OutputValidatingRunner.
    """

    runner_type = "fake_review"

    def __init__(self, store: ArtifactStore, *, verdicts=None, results=None, emit_invalid=False,
                 runner_type="fake_review"):
        self.store = store
        self.runner_type = runner_type
        self.verdicts = list(verdicts or [])
        self.results = list(results or [])
        self.emit_invalid = emit_invalid
        self.roles = {Role.STORY_WRITER.value}
        self.invocations: list[TaskPacket] = []

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED

    def _next_verdict(self) -> str:
        verdict = self.verdicts.pop(0) if self.verdicts else "approve"
        if verdict not in VERDICTS:
            raise ValueError(f"unknown scripted verdict {verdict!r}")
        return verdict

    def execute(self, packet: TaskPacket) -> RunnerResult:
        self.invocations.append(packet)
        item = self.results.pop(0) if self.results else ResultCode.SUCCESS
        if isinstance(item, _CrashSentinel):
            raise item.exc
        if item is TIMEOUT:
            raise item
        if isinstance(item, RunnerResult):
            if item.code is not ResultCode.SUCCESS:
                return item
        elif item is not ResultCode.SUCCESS:
            return RunnerResult(code=item, metrics={"invocations": len(self.invocations)})

        if (packet.task_config or {}).get("step") != STEP:
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="unknown_step",
                                error_message=f"unsupported step {(packet.task_config or {}).get('step')!r}")
        try:
            review_rel, revised_rel = _review_outputs(packet, self.store)
            story = self.store.read(packet.inputs["story_artifact"]).decode("utf-8")
        except OutputPathError as exc:
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="bad_output_path", error_message=str(exc))
        except (FileNotFoundError, KeyError):
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="missing_input",
                                error_message="input artifact not found")
        verdict = self._next_verdict()
        written = []
        if self.emit_invalid:
            self.store.write(review_rel, b"{ this is not json")
            written.append(review_rel)
        else:
            issues = [] if verdict == "approve" else [
                {"aspect": "style", "severity": "medium", "note": "Tighten the middle scenes."}]
            review = {"version": 1, "verdict": verdict,
                      "summary": "The story is consistent with the canon." if verdict == "approve"
                      else "The story needs a revision pass.", "issues": issues}
            self.store.write(review_rel, json.dumps(review, ensure_ascii=False, indent=2).encode("utf-8"))
            written.append(review_rel)
            if revised_rel is not None and verdict == "revise":
                revised = story.rstrip("\n") + "\n\nRevised: the rival's motive is now spelled out before the end.\n"
                self.store.write(revised_rel, revised.encode("utf-8"))
                written.append(revised_rel)
        return RunnerResult(code=ResultCode.SUCCESS, artifacts={"outputs": written},
                            metrics={"invocations": len(self.invocations)})


__all__ = ["FakeReviewRunner", "REVIEW_VALIDATORS", "ReviewStep", "review_path", "revised_story_path",
           "validate_review", "validate_review_output"]
