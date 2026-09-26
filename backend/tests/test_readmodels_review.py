"""Read models for the review step (roadmap 4.4): the optional "review" step in the pipeline summary, the latest
review round, revisions and the workflow preset. Rows are inserted directly; the review handler is a TEST-ONLY stub
whose enabled()/status() the test controls (the real ReviewStep is covered in test_review_steps.py)."""

import json
from datetime import datetime, timedelta

import pytest

from storyflow.artifacts import ArtifactStore
from storyflow.models import (
    ChannelWorkflow, PipelineJob, StoryProject, StoryReview, StoryVersion,
)
from storyflow.pipeline import PipelineContext, StepHandler, StepStatus, StepView
from storyflow.readmodels import ReadModels, categorize_error, to_jsonable
from storyflow.story_steps import CanonStep, SourceStep, StoryStep
from storyflow.tts_steps import AudioStep, TTSStep

T0 = datetime(2026, 6, 1, 10, 0, 0)


class ReviewStub(StepHandler):
    """Controls whether the review step exists (enabled) and what it reports (view)."""

    step = "review"
    job_kind = "story_review"
    role = "story_writer"

    def __init__(self, enabled=True, view=None):
        self.is_enabled = enabled
        self.view = view or StepView(StepStatus.NOT_STARTED)

    def enabled(self, db, ctx, project):
        return self.is_enabled

    def status(self, db, ctx, project):
        return self.view

    def begin(self, db, ctx, project):
        return None

    def link_job(self, db, ctx, domain_id, job):
        pass

    def finalize(self, db, ctx, domain_id, job):
        pass

    def mark_failed(self, db, ctx, domain_id, job):
        pass


@pytest.fixture
def ctx(session_factory, tmp_path):
    return PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "a"),
                           subtitle_client=None, clock=lambda: T0)


def make_rm(ctx, stub):
    chain = [SourceStep(), CanonStep(), StoryStep(), stub, TTSStep(), AudioStep()]   # review sits after story
    return ReadModels(ctx, chain)


def make_project(db, config=None, name="w"):
    wf = ChannelWorkflow(name=name, mode="auto", status="active", config=config if config is not None else {})
    db.add(wf)
    db.commit()
    p = StoryProject(title="Tale", slug=f"tale-{wf.id[:6]}", channel_workflow_id=wf.id)
    db.add(p)
    db.commit()
    return wf, p


def add_version(db, project, number, status="active", path=None):
    v = StoryVersion(story_project_id=project.id, version_number=number, title=f"v{number}", content="x " * 5,
                     word_count=5, content_path=path or f"projects/{project.id}/story/g{number}/story.md",
                     status=status)
    db.add(v)
    db.commit()
    return v


def add_review(db, project, version, *, round_number=1, status="completed", verdict="approve", summary="ok",
               findings=None, revised=None, created=T0, error_code=None):
    r = StoryReview(story_project_id=project.id, story_version_id=version.id, round_number=round_number,
                    status=status, verdict=verdict, summary=summary, findings=findings, revised_version_id=revised,
                    error_code=error_code, config={}, created_at=created, updated_at=created)
    db.add(r)
    db.commit()
    return r


# --- the review step in the pipeline summary --------------------------------------------------


def test_review_step_is_hidden_when_disabled(db, ctx):
    _, p = make_project(db)
    snap = make_rm(ctx, ReviewStub(enabled=False)).get_project(p.id)
    assert [s.step for s in snap.steps] == ["source", "canon", "story", "tts", "audio"]
    assert snap.review is None and snap.revision_count == 0


def test_review_step_appears_between_story_and_tts_when_enabled(db, ctx):
    _, p = make_project(db)
    snap = make_rm(ctx, ReviewStub(enabled=True)).get_project(p.id)
    assert [s.step for s in snap.steps] == ["source", "canon", "story", "review", "tts", "audio"]


def test_disabled_review_never_becomes_the_current_step(db, ctx):
    _, p = make_project(db)
    stub = ReviewStub(enabled=False, view=StepView(StepStatus.NOT_STARTED))   # would block if it were counted
    snap = make_rm(ctx, stub).get_project(p.id)
    assert snap.current_step == "source"
    stub.is_enabled = True
    assert make_rm(ctx, stub).get_project(p.id).current_step == "source"     # earlier steps still come first


def test_a_running_review_is_the_current_step_and_shows_its_job(db, ctx):
    _, p = make_project(db)
    v = add_version(db, p, 1)
    review = add_review(db, p, v, status="processing", verdict=None, summary=None)
    db.add(PipelineJob(kind="story_review", role="story_writer", status="processing",
                       dedupe_key=f"review:{review.id}", payload_json={}))
    db.commit()
    stub = ReviewStub(view=StepView(StepStatus.IN_PROGRESS, domain_id=review.id))
    rm = make_rm(ctx, stub)
    snap = rm.get_project(p.id)
    step = next(s for s in snap.steps if s.step == "review")
    assert step.status == "in_progress" and step.domain_id == review.id
    assert step.job is not None and step.job.kind == "story_review" and step.job.status == "processing"
    assert snap.review.status == "processing" and snap.review.verdict is None


# --- the latest review round ---------------------------------------------------------------------


def test_no_review_rows_means_no_review_info(db, ctx):
    _, p = make_project(db)
    add_version(db, p, 1)
    assert make_rm(ctx, ReviewStub()).get_project(p.id).review is None


def test_latest_round_wins_even_if_it_was_created_earlier(db, ctx):
    _, p = make_project(db)
    v1, v2 = add_version(db, p, 1), add_version(db, p, 2)
    add_review(db, p, v2, round_number=2, verdict="approve", summary="round two", created=T0)
    add_review(db, p, v1, round_number=1, verdict="revise", summary="round one", created=T0 + timedelta(hours=1))
    info = make_rm(ctx, ReviewStub()).get_project(p.id).review
    assert (info.round_number, info.summary, info.verdict) == (2, "round two", "approve")


def test_same_round_ties_are_broken_by_creation_time(db, ctx):
    _, p = make_project(db)
    v = add_version(db, p, 1)
    add_review(db, p, v, round_number=1, status="failed", verdict=None, summary="first try", created=T0,
               error_code="invalid_review")
    add_review(db, p, v, round_number=1, status="completed", summary="second try",
               created=T0 + timedelta(minutes=5))
    info = make_rm(ctx, ReviewStub()).get_project(p.id).review
    assert info.summary == "second try" and info.status == "completed"


def test_failed_review_reports_its_error_code(db, ctx):
    _, p = make_project(db)
    v = add_version(db, p, 1)
    add_review(db, p, v, status="failed", verdict=None, summary=None, error_code="invalid_review")
    info = make_rm(ctx, ReviewStub()).get_project(p.id).review
    assert (info.status, info.verdict, info.error_code) == ("failed", None, "invalid_review")


def test_review_of_another_project_is_not_shown(db, ctx):
    _, p1 = make_project(db, name="a")
    _, p2 = make_project(db, name="b")
    add_review(db, p1, add_version(db, p1, 1), summary="mine")
    assert make_rm(ctx, ReviewStub()).get_project(p2.id).review is None


def test_issues_are_reported_capped_and_scrubbed(db, ctx):
    _, p = make_project(db)
    v = add_version(db, p, 1)
    findings = [{"aspect": "canon", "severity": "high", "note": f"issue {i}"} for i in range(25)]
    findings[0]["note"] = "see C:\\Users\\ming\\secret\\canon.json for details " + "x" * 500
    findings.insert(1, "not a dict")                                   # malformed rows are ignored
    findings.insert(2, {"aspect": "style"})                            # missing fields get safe defaults
    add_review(db, p, v, findings=findings, summary="s" * 900)
    info = make_rm(ctx, ReviewStub()).get_project(p.id).review
    assert info.issue_count == 26 and len(info.issues) == 20
    first = info.issues[0]
    assert "<path>" in first["note"] and "C:\\" not in first["note"] and len(first["note"]) <= 400
    assert info.issues[1] == {"aspect": "style", "severity": "low", "note": ""}
    assert len(info.summary) == 400


def test_revised_flag_and_version_id(db, ctx):
    _, p = make_project(db)
    v1, v2 = add_version(db, p, 1), add_version(db, p, 2)
    add_review(db, p, v1, verdict="revise", revised=v2.id)
    info = make_rm(ctx, ReviewStub()).get_project(p.id).review
    assert info.revised is True and info.revised_version_id == v2.id and info.verdict == "revise"
    plain = make_project(db, name="c")[1]
    add_review(db, plain, add_version(db, plain, 1), verdict="approve")
    assert make_rm(ctx, ReviewStub()).get_project(plain.id).review.revised is False


# --- revisions -------------------------------------------------------------------------------------


@pytest.mark.parametrize("versions,expected", [
    ([], 0),
    ([("active", 1)], 0),
    ([("active", 1), ("active", 2)], 1),
    ([("active", 1), ("active", 2), ("active", 3)], 2),
    ([("active", 1), ("superseded", 2), ("abandoned", 3)], 0),     # only active versions count
])
def test_revision_count_is_active_versions_beyond_the_first(db, ctx, versions, expected):
    _, p = make_project(db)
    for status, number in versions:
        add_version(db, p, number, status=status)
    assert make_rm(ctx, ReviewStub()).get_project(p.id).revision_count == expected


def test_story_version_is_still_the_latest_active_version(db, ctx):
    _, p = make_project(db)
    add_version(db, p, 1)
    v2 = add_version(db, p, 2, path=f"projects/{p.id}/review/r/story_revised.md")
    snap = make_rm(ctx, ReviewStub()).get_project(p.id)
    assert snap.story_version.version_number == 2 and snap.story_version.id == v2.id
    assert snap.story_version.content_path == f"projects/{p.id}/review/r/story_revised.md"


# --- preset ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("config,expected", [
    ({"preset": "quality"}, "quality"), ({"preset": "fast", "review": {"enabled": False}}, "fast"),
    ({}, None), ({"preset": 5}, None), ({"preset": ""}, None), ({"preset": ["x"]}, None),
    ({"preset": "p" * 80}, "p" * 32),
])
def test_workflow_preset_is_exposed_in_snapshot_and_summary(db, ctx, config, expected):
    wf, _ = make_project(db, config=config)
    rm = make_rm(ctx, ReviewStub())
    assert rm.get_workflow(wf.id).preset == expected
    assert next(w for w in rm.list_workflows() if w.id == wf.id).preset == expected


def test_preset_survives_the_json_round_trip(db, ctx):
    wf, p = make_project(db, config={"preset": "balanced"})
    v = add_version(db, p, 1)
    add_review(db, p, v, findings=[{"aspect": "logic", "severity": "medium", "note": "gap"}])
    payload = to_jsonable(make_rm(ctx, ReviewStub()).get_workflow(wf.id))
    assert json.loads(json.dumps(payload))["preset"] == "balanced"
    project = payload["projects"][0]
    assert project["revision_count"] == 0
    assert project["review"] == {
        "id": project["review"]["id"], "round_number": 1, "status": "completed", "verdict": "approve",
        "summary": "ok", "issue_count": 1, "issues": [{"aspect": "logic", "severity": "medium", "note": "gap"}],
        "revised": False, "revised_version_id": None, "error_code": None}
    listed = to_jsonable(make_rm(ctx, ReviewStub()).list_workflows())
    assert listed[0]["preset"] == "balanced"


def test_review_error_codes_are_business_failures():
    assert categorize_error("invalid_review") == "business" == categorize_error("review_failed")
