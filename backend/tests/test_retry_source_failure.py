"""retry(project_id) for an inline (source-step) failure: response and state must agree."""

import pytest

from storyflow.errors import NotRetryable
from test_phase5_integration import CONFIG, Stack


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.app.close()


def _failed_source_workflow(stack):
    cfg = {**CONFIG, "source": {"video_id": "does-not-exist", "languages": ["en"]}}
    wf = stack.workflows.create_workflow("bad-source", config=cfg).workflow_id
    project = stack.workflows.add_project(wf, "P").detail["project_id"]
    stack.workflows.start(wf)
    stack.assign_discovered_runner(wf)
    snap = stack.drive(wf, lambda s: s.display_state == "failed")
    return wf, project, snap


def test_retry_with_project_id_for_source_failure_is_consistent(stack):
    wf, project, snap = _failed_source_workflow(stack)
    assert snap.status_detail["step"] == "source" and snap.status_detail["project_id"] == project
    result = stack.workflows.retry(wf, project_id=project)
    assert result.changed is True and result.detail["step"] == "source" and result.detail["project_id"] == project
    # the (still missing) source fails again, but the command itself was accepted and coherent
    assert stack.drive(wf, lambda s: s.display_state == "failed").status_detail["step"] == "source"


def test_retry_for_a_project_that_did_not_fail_is_rejected_without_state_change(stack):
    wf, _project, _snap = _failed_source_workflow(stack)
    other = stack.workflows.add_project(wf, "Other").detail["project_id"]
    before = stack.read.get_workflow(wf)
    with pytest.raises(NotRetryable) as exc:
        stack.workflows.retry(wf, project_id=other)
    assert exc.value.details["reason"] == "project_not_failed"
    after = stack.read.get_workflow(wf)
    assert after.status == before.status == "paused" and after.status_reason == "step_failed"
    assert after.status_detail == before.status_detail  # no leftover retry marker, not reactivated
