"""The real ClaudeCliRunner (over the fake CLI) and the ReviewStep validators must agree: canon -> story -> review
(quality preset) through the real Dispatcher path, with the revised story becoming the newest StoryVersion."""

import pytest
from sqlalchemy import select

from storyflow.models import ChannelWorkflow, StoryReview, StoryVersion
from storyflow.pipeline import OutputValidatingRunner, StepStatus
from storyflow.review_steps import REVIEW_VALIDATORS, ReviewStep
from storyflow.story_steps import CanonStep, StoryStep

from test_claude_cli import make_runner, run_canon, wrapped  # noqa: F401
from test_story_steps import (  # noqa: F401  (fixtures + helpers of the Phase 4 dispatcher-path tests)
    ctx, fresh, make_ctx, make_snapshot, project, reg_for, reload_job, run_job, session, store,
)


def review_runner(runner, store):
    return OutputValidatingRunner(runner, store, REVIEW_VALIDATORS)


def enable_review(db, project, **review):
    wf = db.get(ChannelWorkflow, project.channel_workflow_id)
    wf.config = {**wf.config, "review": {"enabled": True, "revise": True, "max_rounds": 2, **review}}
    db.commit()


def written_story(db, ctx, project, store, session, tmp_path, monkeypatch):
    good = make_runner(store, tmp_path, monkeypatch)
    run_canon(db, ctx, project, store, session, good)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(good, store), spec)
    step.finalize(db, ctx, gid, job)
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED


def run_review(db, ctx, project, store, session, runner):
    step = ReviewStep()
    begun = step.begin(db, ctx, project)
    assert begun is not None
    rid, spec = begun
    job, _ = run_job(db, session, review_runner(runner, store), spec)
    step.finalize(db, ctx, rid, job)
    return rid, job


def test_quality_review_with_the_real_runner_revises_then_approves(db, ctx, project, store, session, tmp_path,
                                                                    monkeypatch):
    enable_review(db, project)
    written_story(db, ctx, project, store, session, tmp_path, monkeypatch)
    assert ReviewStep().enabled(db, ctx, project) and ReviewStep().status(db, ctx, project).status is \
        StepStatus.NOT_STARTED

    rid1, job1 = run_review(db, ctx, project, store, session, make_runner(store, tmp_path, monkeypatch, "review_revise"))
    assert reload_job(db, job1.id).status == "completed"
    r1 = fresh(db, StoryReview, rid1)
    assert (r1.status, r1.verdict, r1.round_number) == ("completed", "revise", 1)
    assert r1.summary and r1.findings and r1.revised_version_id
    versions = db.scalars(select(StoryVersion).where(StoryVersion.story_project_id == project.id)
                          .order_by(StoryVersion.version_number)).all()
    assert [v.version_number for v in versions] == [1, 2]
    v1, v2 = versions
    assert r1.revised_version_id == v2.id and v2.content_path.endswith("/story_revised.md")
    assert len(v2.content.split()) >= len(v1.content.split()) * 0.85
    assert store.read(v2.content_path).decode("utf-8") == v2.content

    # the corrected version is what gets reviewed next (round 2), and an approval finishes the step
    assert ReviewStep().status(db, ctx, project).status is StepStatus.NOT_STARTED
    rid2, _ = run_review(db, ctx, project, store, session, make_runner(store, tmp_path, monkeypatch, "review_approve"))
    r2 = fresh(db, StoryReview, rid2)
    assert (r2.verdict, r2.round_number, r2.story_version_id, r2.revised_version_id) == ("approve", 2, v2.id, None)
    assert ReviewStep().status(db, ctx, project).status is StepStatus.COMPLETED
    assert ReviewStep().begin(db, ctx, project) is None      # nothing left to review
    assert len(db.scalars(select(StoryVersion).where(StoryVersion.story_project_id == project.id)).all()) == 2


def test_balanced_review_records_the_verdict_and_leaves_the_story_alone(db, ctx, project, store, session, tmp_path,
                                                                        monkeypatch):
    enable_review(db, project, revise=False, max_rounds=1)
    written_story(db, ctx, project, store, session, tmp_path, monkeypatch)
    runner = make_runner(store, tmp_path, monkeypatch, "review_revise")   # the model tries to volunteer a rewrite
    rid, _ = run_review(db, ctx, project, store, session, runner)
    review = fresh(db, StoryReview, rid)
    assert review.status == "completed" and review.revised_version_id is None
    assert len(db.scalars(select(StoryVersion).where(StoryVersion.story_project_id == project.id)).all()) == 1
    assert ReviewStep().status(db, ctx, project).status is StepStatus.COMPLETED


@pytest.mark.parametrize("mode", ["review_bad_json", "review_bad_verdict", "review_no_delimiter"])
def test_a_bad_answer_is_a_business_failure_and_writes_nothing(db, ctx, project, store, session, tmp_path,
                                                                 monkeypatch, mode):
    enable_review(db, project)
    written_story(db, ctx, project, store, session, tmp_path, monkeypatch)
    step = ReviewStep()
    rid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, review_runner(make_runner(store, tmp_path, monkeypatch, mode), store), spec)
    j = reload_job(db, job.id)
    assert j.attempts == 1 and j.status == "queued" and j.infrastructure_failures == 0   # retried as invalid output
    assert not store.exists(f"projects/{project.id}/review/{rid}/review.json")
    assert len(db.scalars(select(StoryVersion).where(StoryVersion.story_project_id == project.id)).all()) == 1
