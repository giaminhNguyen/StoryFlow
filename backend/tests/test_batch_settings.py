"""storyflow.policy batch settings (``batch.max_active``): strict parse, lenient runtime read, and how
WorkflowService.create_workflow validates / records them."""

from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.errors import ValidationFailed
from storyflow.models import ChannelWorkflow
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import PipelineContext
from storyflow.policy import (
    RECOMMENDED, RECOMMENDED_BATCH_SETTINGS, BatchSettings, PolicyError, batch_from_config, parse_batch_settings,
)
from storyflow.services import WorkflowService
from storyflow.story_steps import SourceStep
from storyflow.subtitles import FakeSubtitleClient

NOW = datetime(2026, 6, 1, 8, 0, 0)


# --- pure policy -------------------------------------------------------------------------------


def test_default_is_unbounded_legacy():
    assert parse_batch_settings(None) == BatchSettings() == BatchSettings(None)
    assert parse_batch_settings({}) == BatchSettings()
    assert parse_batch_settings({"max_active": None}) == BatchSettings()


def test_recommended_batch_settings_parse():
    assert RECOMMENDED_BATCH_SETTINGS == {"max_active": 2}
    assert parse_batch_settings(RECOMMENDED_BATCH_SETTINGS) == BatchSettings(2)


@pytest.mark.parametrize("value", [1, 2, 10, 50])
def test_valid_max_active(value):
    assert parse_batch_settings({"max_active": value}).max_active == value


@pytest.mark.parametrize("raw", [
    "2", 2, [1], ["max_active"], True, {"max_active": True}, {"max_active": False}, {"max_active": 0},
    {"max_active": -1}, {"max_active": 51}, {"max_active": 1.5}, {"max_active": 2.0}, {"max_active": "3"},
    {"max_active": [2]}, {"max_active": 10 ** 9}, {"other": 1}, {"max_active": 2, "other": 1},
])
def test_invalid_batch_settings_are_rejected(raw):
    with pytest.raises(PolicyError):
        parse_batch_settings(raw)


def test_unknown_key_message_is_short():
    with pytest.raises(PolicyError) as exc:
        parse_batch_settings({"x" * 500: 1})
    assert len(str(exc.value)) < 200


def test_lenient_runtime_read_degrades_to_the_legacy_default():
    assert batch_from_config({"batch": {"max_active": 3}}).max_active == 3
    assert batch_from_config({"batch": {"max_active": 0}}) == BatchSettings()
    assert batch_from_config({"batch": "two"}) == BatchSettings()
    assert batch_from_config({"batch": {"nope": 1}}) == BatchSettings()
    assert batch_from_config({}) == BatchSettings()
    assert batch_from_config(None) == BatchSettings()
    assert batch_from_config("not a dict") == BatchSettings()


def test_settings_are_immutable():
    with pytest.raises(Exception):
        BatchSettings(2).max_active = 3


# --- WorkflowService.create_workflow -------------------------------------------------------------


@pytest.fixture
def workflows(session_factory, tmp_path):
    ctx = PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "a"),
                          subtitle_client=FakeSubtitleClient({}), clock=lambda: NOW)
    return WorkflowService(ctx, Orchestrator(ctx, dispatcher=None, source=SourceStep(), steps=[]))


def stored_config(session_factory, workflow_id):
    with session_factory() as db:
        return db.get(ChannelWorkflow, workflow_id, populate_existing=True).config


def workflow_count(session_factory):
    with session_factory() as db:
        return len(db.scalars(select(ChannelWorkflow)).all())


def test_create_workflow_records_both_recommended_defaults(workflows, session_factory):
    wf_id = workflows.create_workflow("w", config={"source": {"languages": ["vi"]}}).workflow_id
    cfg = stored_config(session_factory, wf_id)
    assert cfg["batch"] == RECOMMENDED_BATCH_SETTINGS == {"max_active": 2}
    assert cfg["failure_policy"] == RECOMMENDED
    assert cfg["source"] == {"languages": ["vi"]}


@pytest.mark.parametrize("explicit", [{"max_active": 5}, {"max_active": None}, {}, None])
def test_create_workflow_keeps_an_explicit_batch_value(workflows, session_factory, explicit):
    wf_id = workflows.create_workflow("w", config={"batch": explicit}).workflow_id
    assert stored_config(session_factory, wf_id)["batch"] == explicit      # not overwritten by the default


@pytest.mark.parametrize("bad", [{"max_active": 0}, {"max_active": 51}, {"max_active": True}, {"max_active": "2"},
                                 {"bogus": 1}, "two", 2, [1]])
def test_create_workflow_rejects_an_invalid_batch(workflows, session_factory, bad):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config={"batch": bad})
    assert exc.value.details["reason"] == "invalid_batch"
    assert workflow_count(session_factory) == 0                            # nothing was created


def test_invalid_failure_policy_keeps_its_own_reason(workflows, session_factory):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config={"failure_policy": {"on_no_subtitle": "explode"}, "batch": {}})
    assert exc.value.details["reason"] == "invalid_failure_policy"
    assert workflow_count(session_factory) == 0


def test_a_stored_invalid_batch_never_stops_the_engine(session_factory, tmp_path):
    """Config written by hand / an older version: the runtime read degrades to 'all at once' instead of raising."""
    with session_factory() as db:
        wf = ChannelWorkflow(name="x", mode="auto", status="active", config={"batch": {"max_active": -7}})
        db.add(wf)
        db.commit()
        assert batch_from_config(wf.config).max_active is None
