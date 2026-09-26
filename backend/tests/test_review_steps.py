"""Story review + revision (roadmap 4.4): validators, ReviewStep and the fake runner, then the whole pipeline
end to end with the real handlers / dispatcher and the deterministic fake runners (the ``Stack`` of
test_phase5_integration). Frozen clock, no sleeps."""

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.models import (
    CanonAnalysis, ChannelWorkflow, PipelineJob, SourceSnapshot, StoryProject, StoryReview, StoryVersion,
    TTSGeneration,
)
from storyflow.pipeline import PipelineContext, StepStatus
from storyflow.protocol import ResultCode, RunnerResult, TaskPacket
from storyflow.review_steps import (
    REVIEW_VALIDATORS, FakeReviewRunner, ReviewStep, review_path, revised_story_path, validate_review,
    validate_review_output,
)
from storyflow.runtime.app import PipelineRouter
from test_phase5_integration import CONFIG, Stack

NOW = datetime(2026, 4, 1, 10, 0, 0)
STORY = " ".join(f"word{i}" for i in range(120))          # 120 words: revisions must stay >= 85% of it


def review_json(verdict="approve", **over):
    obj = {"version": 1, "verdict": verdict, "summary": "Looks fine.",
           "issues": [] if verdict == "approve" else [{"aspect": "logic", "severity": "high", "note": "Fix the gap."}]}
    obj.update(over)
    return obj


def put_review(store, obj, pid="p1", rid="r1"):
    store.write(review_path(pid, rid), json.dumps(obj).encode("utf-8"))


def packet(*, revise=False, pid="p1", rid="r1", outputs=None, target=None, inputs_extra=None):
    outs = outputs if outputs is not None else (
        [review_path(pid, rid)] + ([revised_story_path(pid, rid)] if revise else []))
    return TaskPacket(task_id="t", role="story_writer",
                      inputs={"project_id": pid, "review_id": rid, **(inputs_extra or {})}, outputs=outs,
                      task_config={"step": "review", "review_id": rid, "revise": revise, "target_length": target})


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


# --- validate_review / validate_review_output ------------------------------------------------


def test_registry_exposes_the_review_validator():
    assert REVIEW_VALIDATORS == {"review": validate_review_output}


@pytest.mark.parametrize("obj", [
    review_json(), review_json("revise"), review_json(issues=[{"aspect": a, "severity": s, "note": "n"}
                                                              for a in ("canon", "logic", "style", "length", "other")
                                                              for s in ("low", "medium", "high")]),
    {**review_json(), "extra": "keys are ignored"}, review_json(summary="x" * 2000),
    review_json(issues=[{"aspect": "style", "severity": "low", "note": "n" * 1000}] * 50),
])
def test_valid_reviews(obj):
    assert validate_review(obj) is None


@pytest.mark.parametrize("obj,needle", [
    ([], "JSON object"), ("x", "JSON object"), (None, "JSON object"),
    ({**review_json(), "version": 2}, "version"), ({**review_json(), "version": True}, "version"),
    ({k: v for k, v in review_json().items() if k != "version"}, "version"),
    (review_json(verdict="maybe"), "verdict"), (review_json(verdict=None), "verdict"),
    (review_json(summary=""), "summary"), (review_json(summary="   "), "summary"), (review_json(summary=5), "summary"),
    (review_json(summary="x" * 2001), "summary"),
    (review_json(issues="none"), "issues"), (review_json(issues=[]) | {"issues": None}, "issues"),
    (review_json(issues=[{"aspect": "style", "severity": "low", "note": "n"}] * 51), "more than"),
    (review_json(issues=["oops"]), "issues[0]"),
    (review_json(issues=[{"aspect": "plot", "severity": "low", "note": "n"}]), "aspect"),
    (review_json(issues=[{"aspect": "style", "severity": "urgent", "note": "n"}]), "severity"),
    (review_json(issues=[{"aspect": "style", "severity": "low", "note": ""}]), "note"),
    (review_json(issues=[{"aspect": "style", "severity": "low", "note": "n" * 1001}]), "longer"),
    (review_json(issues=[{"aspect": "style", "severity": "low"}]), "note"),
])
def test_invalid_reviews(obj, needle):
    assert needle in validate_review(obj)


def test_output_validator_accepts_approve_and_ignores_a_stray_revision(store):
    put_review(store, review_json("approve"))
    assert validate_review_output(packet(), store) is None
    store.write(revised_story_path("p1", "r1"), b"stray")
    assert validate_review_output(packet(), store) is None                       # not requested: never read
    assert validate_review_output(packet(revise=True), store) is None            # approve + revise requested: ignored


def test_output_validator_problems(store):
    assert "review.json was not produced" in validate_review_output(packet(), store)
    store.write(review_path("p1", "r1"), b"{ nope")
    assert "not valid UTF-8 JSON" in validate_review_output(packet(), store)
    store.write(review_path("p1", "r1"), b"\xff\xfe\xfa")
    assert "not valid UTF-8 JSON" in validate_review_output(packet(), store)
    put_review(store, review_json(verdict="maybe"))
    assert "verdict" in validate_review_output(packet(), store)
    put_review(store, review_json(summary=""))
    assert "summary" in validate_review_output(packet(), store)
    put_review(store, review_json(issues=[{"aspect": "style", "severity": "low", "note": "n"}] * 51))
    assert "more than" in validate_review_output(packet(), store)


def test_revise_needs_a_revised_story_that_is_long_enough(store):
    put_review(store, review_json("revise"))
    p = packet(revise=True, target=120)
    assert "story_revised.md was not produced" in validate_review_output(p, store)
    store.write(revised_story_path("p1", "r1"), b"\xff\xfe")
    assert "not valid UTF-8" in validate_review_output(p, store)
    store.write(revised_story_path("p1", "r1"), b"too short")
    assert "fewer than" in validate_review_output(p, store)                      # below the absolute minimum
    store.write(revised_story_path("p1", "r1"), (" ".join(["w"] * 50)).encode())
    assert "too short" in validate_review_output(p, store)                       # < 85% of the reviewed story
    store.write(revised_story_path("p1", "r1"), (" ".join(["w"] * 101)).encode())
    assert "too short" in validate_review_output(p, store)                       # 101 < 0.85 * 120 = 102
    store.write(revised_story_path("p1", "r1"), (" ".join(["w"] * 102)).encode())
    assert validate_review_output(p, store) is None                              # exactly the minimum
    store.write(revised_story_path("p1", "r1"), (" ".join(["w"] * 102) + " /home/user/x").encode())
    assert "absolute path" in validate_review_output(p, store)
    # a "revise" verdict is fine without a revision when the workflow did not ask for one
    assert validate_review_output(packet(revise=False), store) is None


@pytest.mark.parametrize("kwargs,needle", [
    ({"outputs": []}, "packet must list"),
    ({"outputs": ["projects/p1/review/r1/review.json", "projects/p1/review/r1/story_revised.md"]}, "packet must list"),
    ({"revise": True, "outputs": ["projects/p1/review/r1/review.json"]}, "story_revised.md"),
    ({"outputs": [5]}, "packet must list"),
    ({"outputs": ["projects/other/review/r1/review.json"]}, "must be projects/p1/review/r1/review.json"),
    ({"outputs": ["projects/p1/review/other/review.json"]}, "must be projects/p1/review/r1/review.json"),
    ({"outputs": ["projects/p1/story/r1/review.json"]}, "must be projects/p1/review/r1/review.json"),
    ({"outputs": ["projects/p1/review/r1/other.json"]}, "must be projects/p1/review/r1/review.json"),
    ({"outputs": ["projects/p1/review/r1/../../../x/review.json"]}, "unsafe output path"),
    ({"outputs": ["/etc/review.json"]}, "unsafe output path"),
    ({"revise": True, "outputs": ["projects/p1/review/r1/review.json", "projects/p1/review/r1/story.md"]},
     "story_revised.md"),
])
def test_output_validator_rejects_unsafe_or_wrong_paths(store, kwargs, needle):
    assert needle in validate_review_output(packet(**kwargs), store)


def test_output_validator_needs_project_and_review_ids(store):
    no_pid = TaskPacket(inputs={"review_id": "r1"}, outputs=["projects/p1/review/r1/review.json"],
                        task_config={"step": "review", "review_id": "r1"})
    assert "project_id" in validate_review_output(no_pid, store)
    no_rid = TaskPacket(inputs={"project_id": "p1"}, outputs=["projects/p1/review/r1/review.json"],
                        task_config={"step": "review"})
    assert "review_id" in validate_review_output(no_rid, store)


# --- FakeReviewRunner -----------------------------------------------------------------------


def runner_packet(store, *, revise, verdict_story=STORY):
    store.write("projects/p1/story/g/story.md", verdict_story.encode())
    return packet(revise=revise, target=len(verdict_story.split()), inputs_extra={"story_artifact": "projects/p1/story/g/story.md"})


def test_fake_runner_verdicts_are_consumed_in_order_and_default_to_approve(store):
    runner = FakeReviewRunner(store, verdicts=["revise", "approve"])
    p = runner_packet(store, revise=True)
    seen = []
    for _ in range(3):
        assert runner.execute(p).code is ResultCode.SUCCESS
        assert validate_review_output(p, store) is None
        seen.append(json.loads(store.read(review_path("p1", "r1")))["verdict"])
    assert seen == ["revise", "approve", "approve"] and len(runner.invocations) == 3


def test_fake_runner_writes_a_revision_only_when_asked_and_only_for_revise(store):
    runner = FakeReviewRunner(store, verdicts=["revise", "revise", "approve"])
    res = runner.execute(runner_packet(store, revise=True))
    assert res.artifacts["outputs"] == [review_path("p1", "r1"), revised_story_path("p1", "r1")]
    revised = store.read(revised_story_path("p1", "r1")).decode()
    assert revised.startswith(STORY) and len(revised.split()) > len(STORY.split())
    store.delete(revised_story_path("p1", "r1"))
    res = runner.execute(runner_packet(store, revise=False))                     # revise not requested: no 2nd file
    assert res.artifacts["outputs"] == [review_path("p1", "r1")] and not store.exists(revised_story_path("p1", "r1"))
    res = runner.execute(runner_packet(store, revise=True))                      # approve: no revision either
    assert res.artifacts["outputs"] == [review_path("p1", "r1")]


def test_fake_runner_scripted_failures_write_nothing(store):
    runner = FakeReviewRunner(store, results=[ResultCode.TASK_FAILED, RunnerResult(code=ResultCode.TIMEOUT)])
    p = runner_packet(store, revise=False)
    assert runner.execute(p).code is ResultCode.TASK_FAILED and not store.exists(review_path("p1", "r1"))
    assert runner.execute(p).code is ResultCode.TIMEOUT
    assert runner.execute(p).code is ResultCode.SUCCESS and store.exists(review_path("p1", "r1"))


def test_fake_runner_invalid_output_is_caught_by_the_validator(store):
    runner = FakeReviewRunner(store, emit_invalid=True)
    p = runner_packet(store, revise=False)
    assert runner.execute(p).code is ResultCode.SUCCESS
    assert "not valid" in validate_review_output(p, store)


def test_fake_runner_input_and_path_problems(store):
    runner = FakeReviewRunner(store)
    res = runner.execute(packet(revise=False, inputs_extra={"story_artifact": "projects/p1/story/none.md"}))
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "missing_input")
    res = runner.execute(packet(outputs=["projects/p1/elsewhere/review.json"]))
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "bad_output_path")
    other = TaskPacket(inputs={}, outputs=[], task_config={"step": "story"})
    assert runner.execute(other).error_code == "unknown_step"
    with pytest.raises(ValueError):
        FakeReviewRunner(store, verdicts=["shrug"]).execute(runner_packet(store, revise=False))


# --- ReviewStep on its own (no dispatcher) --------------------------------------------------


def make_reviewable(db, store, config, *, story=STORY, canon=True, version=True):
    wf = ChannelWorkflow(name="w", mode="auto", status="active", config=config)
    db.add(wf)
    db.commit()
    project = StoryProject(title="Tale", channel_workflow_id=wf.id)
    db.add(project)
    db.commit()
    snap = SourceSnapshot(story_project_id=project.id, snapshot_number=1, title="t", content="source text",
                          meta={"artifact_path": f"projects/{project.id}/source/0001/source.txt"})
    db.add(snap)
    db.commit()
    store.write(snap.meta["artifact_path"], b"source text")
    if canon:
        db.add(CanonAnalysis(source_snapshot_id=snap.id, status="completed", canon={"x": 1}))
    if version:
        rel = f"projects/{project.id}/story/g1/story.md"
        store.write(rel, story.encode())
        db.add(StoryVersion(story_project_id=project.id, version_number=1, title="Tale", content=story,
                            word_count=len(story.split()), content_path=rel, status="active"))
    db.commit()
    return project


@pytest.fixture
def ctx(session_factory, store):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=None, clock=lambda: NOW)


QUALITY = {"preset": "quality", "review": {"enabled": True, "revise": True, "max_rounds": 2}}


def fresh(db, model, pk):
    return db.get(model, pk, populate_existing=True)


@pytest.mark.parametrize("config,expected", [
    ({}, False), ({"preset": "fast"}, False), ({"preset": "balanced"}, True), ({"preset": "quality"}, True),
    ({"review": {"enabled": True}}, True), ({"preset": "quality", "review": {"enabled": False}}, False),
    ({"review": "garbage"}, False),
])
def test_step_is_enabled_only_when_the_workflow_asks_for_it(db, store, config, expected):
    project = make_reviewable(db, store, config)
    assert ReviewStep().enabled(db, None, project) is expected


def test_status_and_begin_need_a_story_version_and_canon(db, store, ctx):
    step = ReviewStep()
    no_version = make_reviewable(db, store, QUALITY, version=False)
    assert step.status(db, ctx, no_version).status is StepStatus.NOT_STARTED
    assert step.begin(db, ctx, no_version) is None
    no_canon = make_reviewable(db, store, QUALITY, canon=False)
    assert step.begin(db, ctx, no_canon) is None
    assert db.scalars(select(StoryReview)).all() == []


def test_begin_describes_the_job(db, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    review_id, spec = ReviewStep().begin(db, ctx, project)
    version = db.scalar(select(StoryVersion))
    canon = db.scalar(select(CanonAnalysis))
    assert (spec.kind, spec.role, spec.dedupe_key) == ("story_review", "story_writer", f"review:{review_id}")
    inputs = spec.payload["inputs"]
    assert inputs == {"project_id": project.id, "source_artifact": f"projects/{project.id}/source/0001/source.txt",
                      "canon_artifact": f"projects/{project.id}/canon/{canon.id}/canon.json",
                      "story_artifact": version.content_path, "story_version_id": version.id, "review_id": review_id,
                      "round_number": 1, "revise": True, "target_length": 120}
    assert spec.payload["outputs"] == [review_path(project.id, review_id), revised_story_path(project.id, review_id)]
    assert spec.payload["task_config"] == {"step": "review", "review_id": review_id, "revise": True,
                                           "target_length": 120}
    assert spec.payload["skill"]["name"] == "story-branch-writer"
    row = fresh(db, StoryReview, review_id)
    assert (row.status, row.round_number, row.story_version_id) == ("queued", 1, version.id)
    assert row.config == {"revise": True, "max_rounds": 2, "target_length": 120}
    assert ReviewStep().status(db, ctx, project).status is StepStatus.IN_PROGRESS


def test_begin_without_revise_asks_for_review_json_only(db, store, ctx):
    project = make_reviewable(db, store, {"preset": "balanced"})
    _, spec = ReviewStep().begin(db, ctx, project)
    assert len(spec.payload["outputs"]) == 1 and spec.payload["inputs"]["revise"] is False


def test_begin_is_idempotent_while_the_review_is_live(db, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    first, _ = ReviewStep().begin(db, ctx, project)
    second, spec = ReviewStep().begin(db, ctx, project)
    assert first == second == spec.payload["inputs"]["review_id"] and len(db.scalars(select(StoryReview)).all()) == 1


def run_review(db, store, ctx, project, verdict):
    """begin + let the fake runner write the outputs + finalize; returns the review id."""
    step = ReviewStep()
    review_id, spec = step.begin(db, ctx, project)
    p = TaskPacket(inputs=spec.payload["inputs"], outputs=spec.payload["outputs"],
                   task_config=spec.payload["task_config"])
    assert FakeReviewRunner(store, verdicts=[verdict]).execute(p).code is ResultCode.SUCCESS
    assert validate_review_output(p, store) is None
    step.finalize(db, ctx, review_id, None)
    return review_id


def test_finalize_records_the_review_and_creates_the_revised_version_once(db, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    review_id = run_review(db, store, ctx, project, "revise")
    step = ReviewStep()
    review = fresh(db, StoryReview, review_id)
    assert (review.status, review.verdict, review.summary) == ("completed", "revise", "The story needs a revision pass.")
    assert review.findings == [{"aspect": "style", "severity": "medium", "note": "Tighten the middle scenes."}]
    versions = db.scalars(select(StoryVersion).order_by(StoryVersion.version_number)).all()
    assert [v.version_number for v in versions] == [1, 2] and review.revised_version_id == versions[1].id
    assert versions[1].content_path == revised_story_path(project.id, review_id) and versions[1].title == "Tale"
    assert versions[1].word_count == len(versions[1].content.split()) and versions[0].status == "active"
    assert versions[1].content.startswith(STORY)
    step.finalize(db, ctx, review_id, None)                                      # idempotent
    step.finalize(db, ctx, review_id, None)
    assert db.scalar(select(StoryVersion).where(StoryVersion.version_number == 3)) is None
    assert len(db.scalars(select(StoryVersion)).all()) == 2
    # round 2 reviews the NEW version; after approve the rounds are used up and the step is COMPLETED
    assert step.status(db, ctx, project).status is StepStatus.NOT_STARTED
    second = run_review(db, store, ctx, project, "approve")
    assert fresh(db, StoryReview, second).story_version_id == versions[1].id
    assert fresh(db, StoryReview, second).round_number == 2
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED and step.begin(db, ctx, project) is None


def test_revise_verdict_without_revise_enabled_is_only_recorded(db, store, ctx):
    project = make_reviewable(db, store, {"preset": "balanced"})
    review_id = run_review(db, store, ctx, project, "revise")
    review = fresh(db, StoryReview, review_id)
    assert review.verdict == "revise" and review.revised_version_id is None
    assert len(db.scalars(select(StoryVersion)).all()) == 1
    assert ReviewStep().status(db, ctx, project).status is StepStatus.COMPLETED


def test_rounds_exhausted_accepts_the_last_revision_unreviewed(db, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    run_review(db, store, ctx, project, "revise")
    run_review(db, store, ctx, project, "revise")
    assert [v.version_number for v in db.scalars(select(StoryVersion)).all()] == [1, 2, 3]
    assert ReviewStep().status(db, ctx, project).status is StepStatus.COMPLETED
    assert ReviewStep().begin(db, ctx, project) is None


def test_finalize_with_bad_or_missing_output_fails_the_review(db, store, ctx):
    step = ReviewStep()
    project = make_reviewable(db, store, QUALITY)
    review_id, spec = step.begin(db, ctx, project)
    step.finalize(db, ctx, review_id, None)                                      # nothing written yet
    assert (fresh(db, StoryReview, review_id).status, fresh(db, StoryReview, review_id).error_code) == (
        "failed", "missing_output")
    assert step.status(db, ctx, project).status is StepStatus.FAILED

    review_id, spec = step.begin(db, ctx, project)                               # a failed review never blocks
    put_review(store, {"nope": 1}, project.id, review_id)
    step.finalize(db, ctx, review_id, None)
    assert fresh(db, StoryReview, review_id).error_code == "invalid_review"

    review_id, spec = step.begin(db, ctx, project)
    store.write(review_path(project.id, review_id), b"\xff\xfe")
    step.finalize(db, ctx, review_id, None)
    assert fresh(db, StoryReview, review_id).error_code == "invalid_review"

    review_id, spec = step.begin(db, ctx, project)
    put_review(store, review_json("revise"), project.id, review_id)              # revise verdict, no revised file
    step.finalize(db, ctx, review_id, None)
    assert fresh(db, StoryReview, review_id).error_code == "missing_output"

    review_id, spec = step.begin(db, ctx, project)
    put_review(store, review_json("revise"), project.id, review_id)
    store.write(revised_story_path(project.id, review_id), (" ".join(["w"] * 50)).encode())
    step.finalize(db, ctx, review_id, None)
    row = fresh(db, StoryReview, review_id)
    assert (row.status, row.error_code) == ("failed", "invalid_story") and "too short" in row.error_message
    assert len(db.scalars(select(StoryVersion)).all()) == 1                      # nothing was created

    review_id, spec = step.begin(db, ctx, project)                               # ... and a fresh review still works
    put_review(store, review_json("approve"), project.id, review_id)
    step.finalize(db, ctx, review_id, None)
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED


def test_finalize_of_an_unknown_or_failed_review_is_a_no_op(db, store, ctx):
    step = ReviewStep()
    step.finalize(db, ctx, "no-such-review", None)
    project = make_reviewable(db, store, QUALITY)
    review_id, _ = step.begin(db, ctx, project)
    step._fail(db, ctx, review_id, "job_failed", "boom")
    put_review(store, review_json("approve"), project.id, review_id)
    step.finalize(db, ctx, review_id, None)
    assert fresh(db, StoryReview, review_id).status == "failed"


def test_mark_failed_takes_the_job_error(db, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    step = ReviewStep()
    review_id, _ = step.begin(db, ctx, project)

    class Job:
        last_error_code, last_error_message = "invalid_output", "reviewer rambled"

    step.mark_failed(db, ctx, review_id, Job())
    row = fresh(db, StoryReview, review_id)
    assert (row.status, row.error_code, row.error_message) == ("failed", "invalid_output", "reviewer rambled")


def test_two_racing_begins_share_one_live_review(db, session_factory, store, ctx):
    project = make_reviewable(db, store, QUALITY)
    other = session_factory()
    try:
        a, _ = ReviewStep().begin(db, ctx, project)
        b, _ = ReviewStep().begin(other, ctx, other.get(StoryProject, project.id))
    finally:
        other.close()
    assert a == b and len(db.scalars(select(StoryReview)).all()) == 1


# --- the whole pipeline ----------------------------------------------------------------------


class RecordingRouter(PipelineRouter):
    """The fake pipeline, recording the order of the steps it ran and able to poison chosen reviews."""

    def __init__(self, store):
        super().__init__(store)
        self.order: list[str] = []
        self.poison_review: set[str] = set()

    def execute(self, packet):
        step = packet.task_config["step"]
        self.order.append(step)
        if step == "review" and packet.inputs["project_id"] in self.poison_review:
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="poisoned", error_message="poisoned review")
        return super().execute(packet)


class ReviewStack(Stack):
    def __init__(self, tmp_path, *, config=None, projects=1):
        super().__init__(tmp_path, router=RecordingRouter(ArtifactStore(tmp_path / "artifacts")))
        self.wf = self.workflows.create_workflow("w", config={**CONFIG, **(config or {})}).workflow_id
        self.pids = [self.workflows.add_project(self.wf, f"Tale {i}").detail["project_id"] for i in range(projects)]
        self.workflows.start(self.wf)
        self.assign_discovered_runner(self.wf)

    def finish(self, until="completed", **kw):
        return self.drive(self.wf, lambda s: s.display_state == until, **kw)

    def versions(self, pid=None):
        pid = pid or self.pids[0]
        with self.app.session_factory() as db:
            return db.scalars(select(StoryVersion).where(StoryVersion.story_project_id == pid)
                              .order_by(StoryVersion.version_number)).all()

    def reviews(self, pid=None):
        pid = pid or self.pids[0]
        with self.app.session_factory() as db:
            return db.scalars(select(StoryReview).where(StoryReview.story_project_id == pid)
                              .order_by(StoryReview.round_number, StoryReview.created_at)).all()

    def tts_versions(self, pid=None):
        pid = pid or self.pids[0]
        with self.app.session_factory() as db:
            return [t.story_version_id for t in db.scalars(
                select(TTSGeneration).join(StoryVersion, StoryVersion.id == TTSGeneration.story_version_id)
                .where(StoryVersion.story_project_id == pid)).all()]

    def step_names(self, pid=None):
        return [s.step for s in self.read.get_project(pid or self.pids[0]).steps]


@pytest.fixture
def make_stack(tmp_path):
    made = []

    def make(**kw):
        s = ReviewStack(tmp_path, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.app.close()


def test_fast_preset_leaves_the_pipeline_exactly_as_before(make_stack):
    s = make_stack()                                       # no preset given: create_workflow records "fast"
    s.finish()
    assert s.router.order == ["canon", "story", "tts", "audio"]
    assert s.step_names() == ["source", "canon", "story", "tts", "audio"]        # review is invisible
    assert s.reviews() == [] and len(s.versions()) == 1
    with s.app.session_factory() as db:
        assert "story_review" not in {j.kind for j in db.scalars(select(PipelineJob)).all()}


def test_balanced_reviews_between_story_and_tts_without_touching_the_story(make_stack):
    s = make_stack(config={"preset": "balanced"})
    s.finish()
    assert s.router.order == ["canon", "story", "review", "tts", "audio"]
    assert s.step_names() == ["source", "canon", "story", "review", "tts", "audio"]
    (review,) = s.reviews()
    assert (review.status, review.verdict, review.round_number, review.revised_version_id) == (
        "completed", "approve", 1, None)
    assert [v.version_number for v in s.versions()] == [1] and s.tts_versions() == [s.versions()[0].id]
    step = next(x for x in s.read.get_project(s.pids[0]).steps if x.step == "review")
    assert step.status == "completed" and step.job.kind == "story_review" and step.job.role == "story_writer"


def test_balanced_records_a_revise_verdict_but_keeps_the_story(make_stack):
    s = make_stack(config={"preset": "balanced"})
    s.router.review.verdicts = ["revise"]
    s.finish()
    (review,) = s.reviews()
    assert review.verdict == "revise" and review.revised_version_id is None
    assert review.findings == [{"aspect": "style", "severity": "medium", "note": "Tighten the middle scenes."}]
    assert len(s.versions()) == 1 and s.router.order.count("review") == 1
    assert s.router.review.invocations[0].task_config["revise"] is False           # revision was not requested


def test_quality_revision_becomes_the_story_that_tts_reads(make_stack):
    s = make_stack(config={"preset": "quality"})
    s.router.review.verdicts = ["revise", "approve"]
    s.finish()
    v1, v2 = s.versions()
    r1, r2 = s.reviews()
    assert (r1.round_number, r1.story_version_id, r1.verdict, r1.revised_version_id) == (1, v1.id, "revise", v2.id)
    assert (r2.round_number, r2.story_version_id, r2.verdict, r2.revised_version_id) == (2, v2.id, "approve", None)
    assert v2.content.startswith(v1.content.rstrip("\n")) and v2.content_path.endswith("/story_revised.md")
    assert v1.status == "active" and v2.title == v1.title
    assert s.tts_versions() == [v2.id]                                              # TTS used the revision
    assert s.router.order == ["canon", "story", "review", "review", "tts", "audio"]
    assert s.read.get_project(s.pids[0]).story_version.version_number == 2
    # the second review asked about the NEW version
    assert s.router.review.invocations[1].inputs["story_version_id"] == v2.id
    assert s.router.review.invocations[1].inputs["round_number"] == 2


def test_quality_with_approve_on_the_first_round_needs_only_one_review(make_stack):
    s = make_stack(config={"preset": "quality"})
    s.finish()
    assert len(s.reviews()) == 1 and len(s.versions()) == 1 and s.router.order.count("review") == 1


def test_rounds_exhausted_with_revise_on_the_last_round_still_completes(make_stack):
    s = make_stack(config={"preset": "quality"})
    s.router.review.verdicts = ["revise", "revise"]
    s.finish()
    assert [v.version_number for v in s.versions()] == [1, 2, 3] and len(s.reviews()) == 2
    assert s.tts_versions() == [s.versions()[2].id]                                 # the last revision is accepted
    assert s.router.order == ["canon", "story", "review", "review", "tts", "audio"]   # no third review


def test_explicit_review_block_beats_the_preset(make_stack):
    s = make_stack(config={"preset": "quality", "review": {"enabled": False}})
    s.finish()
    assert s.reviews() == [] and "review" not in s.router.order


def test_a_review_that_keeps_failing_pauses_the_workflow_by_default(make_stack):
    s = make_stack(config={"preset": "balanced"})
    s.router.poison_review = set(s.pids)
    snap = s.finish("failed")
    assert snap.status == "paused" and snap.status_detail["project_id"] == s.pids[0]
    assert snap.status_detail["step"] == "review"
    (review,) = s.reviews()
    assert review.status == "failed" and review.error_code
    project = s.read.get_project(s.pids[0])
    assert project.state == "failed" and project.current_step == "review" and project.failure.step == "review"
    assert "tts" not in s.router.order


def test_a_failed_review_ends_only_that_project_under_continue(make_stack):
    s = make_stack(config={"preset": "balanced", "failure_policy": {"on_permanent_error": "continue"}})
    s.router.poison_review = set(s.pids)
    snap = s.finish()                                                                # nothing else to wait for
    assert snap.status == "finished"
    project = s.read.get_project(s.pids[0])
    assert project.state == "needs_attention" and project.status == "needs_attention"
    assert project.status_detail["step"] == "review" and project.status_reason
    assert "tts" not in s.router.order


def test_retry_after_a_failed_review_starts_a_fresh_review_and_completes(make_stack):
    s = make_stack(config={"preset": "balanced"})
    s.router.poison_review = set(s.pids)
    s.finish("failed")
    s.router.poison_review = set()                                                   # the cause is fixed
    s.workflows.retry(s.wf, project_id=s.pids[0])
    s.finish()
    failed, done = s.reviews()[0], s.reviews()[-1]
    assert len(s.reviews()) == 2 and failed.status == "failed" and done.status == "completed"
    assert failed.id != done.id and s.router.order[-2:] == ["tts", "audio"]


def test_reactivating_a_needs_attention_review_runs_it_again(make_stack):
    s = make_stack(config={"preset": "quality", "failure_policy": {"on_permanent_error": "continue"}})
    s.router.poison_review = set(s.pids)
    s.finish()
    assert s.read.get_project(s.pids[0]).state == "needs_attention"
    s.router.poison_review = set()
    s.router.review.verdicts = ["revise", "approve"]
    assert s.app.orchestrator.reactivate_project(s.pids[0]) == "review"
    s.finish()
    assert [r.status for r in s.reviews()] == ["failed", "completed", "completed"]
    assert len(s.versions()) == 2 and s.tts_versions() == [s.versions()[1].id]


def test_a_poisoned_review_does_not_block_the_other_projects(make_stack):
    s = make_stack(config={"preset": "balanced", "failure_policy": {"on_permanent_error": "continue"},
                           "batch": {"max_active": None}}, projects=3)
    s.router.poison_review = {s.pids[1]}
    snap = s.finish()
    states = {p.id: p.state for p in snap.projects}
    assert states == {s.pids[0]: "completed", s.pids[1]: "needs_attention", s.pids[2]: "completed"}
    assert snap.status == "finished" and snap.counts.completed == 2 and snap.counts.needs_attention == 1
    assert s.reviews(s.pids[0])[0].verdict == "approve" and s.reviews(s.pids[2])[0].verdict == "approve"
    assert s.step_names(s.pids[1])[-1] == "audio" and s.read.get_project(s.pids[1]).status_detail["step"] == "review"


def test_a_scripted_runner_failure_is_retried_within_the_job(make_stack):
    s = make_stack(config={"preset": "balanced"})
    s.router.review.results = [ResultCode.TASK_FAILED, ResultCode.INVALID_OUTPUT]   # 2 bad attempts, then fine
    s.finish()
    (review,) = s.reviews()
    assert review.status == "completed" and len(s.router.review.invocations) == 3
