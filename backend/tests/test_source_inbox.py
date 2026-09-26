"""Per-project source settings, the subtitle inbox and the provider / doctor / runtime wiring of multi-source
ingestion (roadmap 4.1). The SourceService itself is covered in test_source_service.py."""

import sys
from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.models import ChannelWorkflow, SourceSnapshot, StoryProject
from storyflow.pipeline import PipelineContext, StepStatus
from storyflow.providers import ProviderConfig, build_provider_stack, load_provider_config
from storyflow.sources import YtDlpLister
from storyflow.story_steps import SourceStep
from storyflow.subtitles import BlockedByProvider, FakeSubtitleClient

NOW = datetime(2026, 5, 1, 9, 0, 0)
VID = "abcdefghijk"          # a valid 11-character YouTube id (the inbox only looks up real ids)
VID_CONFIG = {"source": {"video_id": VID, "languages": ["en"]}}


def track(code="en", text="hello world"):
    return {"language": code.upper(), "language_code": code, "is_generated": False, "is_translatable": True,
            "snippets": [{"text": text, "start": 0.0}, {"text": "second line", "start": 1.0}]}


class Counting(FakeSubtitleClient):
    def __init__(self, store, raises=None):
        super().__init__(store)
        self.calls = []
        self.raises = raises

    def fetch(self, video_id, languages=None, *args, **kwargs):
        self.calls.append((video_id, languages))
        if self.raises:
            raise self.raises
        return super().fetch(video_id, languages, *args, **kwargs)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def make_ctx(session_factory, store, client, inbox_dir=None):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=client,
                           clock=lambda: NOW, inbox_dir=inbox_dir)


def make_project(db, source_config=None, wf_config=None):
    wf = ChannelWorkflow(name="w", mode="auto", status="active",
                         config=wf_config if wf_config is not None else {"source": {"video_id": "vid", "languages": ["en"]}})
    db.add(wf)
    db.commit()
    p = StoryProject(title="P", channel_workflow_id=wf.id, source_config=source_config)
    db.add(p)
    db.commit()
    return p


def snapshot(db, project_id):
    return db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project_id), execution_options={
        "populate_existing": True})


# --- per-project source settings ---------------------------------------------------------------


def test_project_source_config_overrides_the_workflow_source(db, session_factory, store):
    client = Counting({"vid": {"tracks": [track()]}, "other": {"tracks": [track(text="from the other video")]}})
    project = make_project(db, {"kind": "video", "video_id": "other"})
    view = SourceStep().run(make_ctx(session_factory, store, client), project.id)
    assert view.status is StepStatus.COMPLETED
    assert client.calls == [("other", ["en"])]                 # video from the project, languages from the workflow
    assert snapshot(db, project.id).meta["video_id"] == "other"
    assert "from the other video" in store.read(snapshot(db, project.id).meta["artifact_path"]).decode()


def test_project_languages_win_over_workflow_languages(db, session_factory, store):
    client = Counting({"vid": {"tracks": [track("vi", "xin chào")]}})
    project = make_project(db, {"kind": "video", "video_id": "vid", "languages": ["vi"]})
    assert SourceStep().run(make_ctx(session_factory, store, client), project.id).status is StepStatus.COMPLETED
    assert client.calls == [("vid", ["vi"])]


def test_workflow_without_a_source_block_works_with_per_project_sources(db, session_factory, store):
    client = Counting({"vid": {"tracks": [track()]}})
    project = make_project(db, {"kind": "video", "video_id": "vid"}, wf_config={})
    assert SourceStep().run(make_ctx(session_factory, store, client), project.id).status is StepStatus.COMPLETED


# --- the inbox ----------------------------------------------------------------------------------


def test_an_inbox_file_named_after_the_video_beats_the_provider(db, session_factory, store, inbox):
    (inbox / f"{VID}.txt").write_text("Lời truyện do tôi tự gõ.\nDòng hai.", encoding="utf-8")
    client = Counting({VID: {"tracks": [track()]}}, raises=BlockedByProvider("IP blocked"))
    project = make_project(db, wf_config=VID_CONFIG)
    view = SourceStep().run(make_ctx(session_factory, store, client, inbox), project.id)
    assert view.status is StepStatus.COMPLETED and client.calls == []      # provider never touched
    snap = snapshot(db, project.id)
    assert snap.meta["provider"] == "InboxFile" and snap.meta["inbox_file"] == f"{VID}.txt"
    assert snap.meta["video_id"] == VID and snap.content == "Lời truyện do tôi tự gõ.\nDòng hai."
    assert store.read(snap.meta["artifact_path"]).decode("utf-8") == snap.content


def test_no_inbox_file_falls_through_to_the_provider(db, session_factory, store, inbox):
    client = Counting({VID: {"tracks": [track()]}})
    project = make_project(db, wf_config=VID_CONFIG)
    assert SourceStep().run(make_ctx(session_factory, store, client, inbox), project.id).status is StepStatus.COMPLETED
    assert client.calls and snapshot(db, project.id).meta["provider"] == "Counting"


def test_an_inbox_file_is_only_looked_up_for_real_video_ids(db, session_factory, store, inbox):
    (inbox / "vid.txt").write_text("should never be used", encoding="utf-8")   # "vid" is not an 11-char id
    client = Counting({"vid": {"tracks": [track()]}})
    project = make_project(db)
    assert SourceStep().run(make_ctx(session_factory, store, client, inbox), project.id).status is StepStatus.COMPLETED
    assert client.calls and snapshot(db, project.id).meta["provider"] == "Counting"


def test_local_source_reads_an_srt_and_strips_timings(db, session_factory, store, inbox):
    (inbox / "tap 1.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nXin chào\n\n2\n00:00:02,000 --> 00:00:03,500\n<i>Tạm biệt</i>\n",
        encoding="utf-8")
    project = make_project(db, {"kind": "local", "file": "tap 1.srt"}, wf_config={})
    client = Counting({})
    view = SourceStep().run(make_ctx(session_factory, store, client, inbox), project.id)
    assert view.status is StepStatus.COMPLETED and client.calls == []
    snap = snapshot(db, project.id)
    assert snap.content == "Xin chào\nTạm biệt" and snap.meta["video_id"] is None
    assert snap.meta["provider"] == "InboxFile" and snap.meta["inbox_file"] == "tap 1.srt"


@pytest.mark.parametrize("file", ["missing.txt", "../secret.txt", "sub/dir.txt", "notes.doc", ""])
def test_local_source_problems_are_a_clear_error(db, session_factory, store, inbox, file):
    project = make_project(db, {"kind": "local", "file": file}, wf_config={})
    view = SourceStep().run(make_ctx(session_factory, store, Counting({}), inbox), project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "inbox_file_missing")
    assert db.get(StoryProject, project.id, populate_existing=True).status == "active"   # default policy: pause


def test_local_source_without_an_inbox_is_a_clear_error(db, session_factory, store):
    project = make_project(db, {"kind": "local", "file": "a.txt"}, wf_config={})
    view = SourceStep().run(make_ctx(session_factory, store, Counting({}), None), project.id)
    assert view.error_code == "inbox_file_missing"


def test_missing_local_file_ends_only_that_project_under_continue(db, session_factory, store, inbox):
    project = make_project(db, {"kind": "local", "file": "gone.txt"},
                           wf_config={"failure_policy": {"on_permanent_error": "continue"}})
    SourceStep().run(make_ctx(session_factory, store, Counting({}), inbox), project.id)
    p = db.get(StoryProject, project.id, populate_existing=True)
    assert (p.status, p.status_reason) == ("needs_attention", "inbox_file_missing")


def test_a_non_utf8_inbox_file_is_ignored_for_video_sources(db, session_factory, store, inbox):
    (inbox / f"{VID}.txt").write_bytes(b"\xff\xfe\xfa not utf8")
    client = Counting({VID: {"tracks": [track()]}})
    project = make_project(db, wf_config=VID_CONFIG)
    assert SourceStep().run(make_ctx(session_factory, store, client, inbox), project.id).status is StepStatus.COMPLETED
    assert client.calls and snapshot(db, project.id).meta["provider"] == "Counting"


# --- provider configuration --------------------------------------------------------------------


def test_lister_and_inbox_settings_are_read_from_the_environment(tmp_path):
    cfg = load_provider_config(env={"STORYFLOW_YTDLP_PYTHON": "py-x", "STORYFLOW_LISTER_TIMEOUT": "30",
                                    "STORYFLOW_INBOX_DIR": str(tmp_path)}, env_files=[])
    assert (cfg.ytdlp_python, cfg.lister_timeout) == ("py-x", 30.0)
    assert cfg.resolved_inbox_dir() == tmp_path and cfg.problems() == []


def test_defaults_and_bad_lister_timeout():
    cfg = load_provider_config(env={}, env_files=[])
    assert cfg.ytdlp_python == sys.executable and cfg.lister_timeout == 120.0
    assert cfg.resolved_inbox_dir().name == "inbox" and cfg.resolved_inbox_dir().parent.name == "runtime"
    bad = load_provider_config(env={"STORYFLOW_LISTER_TIMEOUT": "soon"}, env_files=[])
    assert any("lister_timeout" in p for p in bad.problems())


def test_provider_stack_carries_a_configured_ytdlp_lister(tmp_path):
    cfg = ProviderConfig(subtitle_provider="none", ytdlp_python="py-y", lister_timeout=45.0)
    lister = build_provider_stack(cfg, ArtifactStore(tmp_path)).video_lister
    assert isinstance(lister, YtDlpLister) and (lister.python, lister.timeout) == ("py-y", 45.0)


def test_build_runtime_wires_the_lister_and_inbox_into_the_pipeline_context(tmp_path):
    from storyflow.runtime import build_runtime
    inbox_dir = tmp_path / "in"
    app = build_runtime(database_url=f"sqlite:///{(tmp_path / 's.db').as_posix()}", artifact_root=tmp_path / "a",
                        ensure_db_schema=True, provider_config=ProviderConfig(subtitle_provider="none",
                                                                              inbox_dir=str(inbox_dir)))
    try:
        assert isinstance(app.ctx.video_lister, YtDlpLister) and app.ctx.inbox_dir == inbox_dir
    finally:
        app.close()


# --- doctor -------------------------------------------------------------------------------------


def test_doctor_reports_whether_channel_links_are_available(monkeypatch):
    import importlib.util
    from storyflow.ops import doctor
    default = ProviderConfig()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object() if name == "yt_dlp" else None)
    assert doctor._lister_check(default).status == doctor.PASS
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None)
    missing = doctor._lister_check(default)
    assert missing.status == doctor.INFO and "requirements-channel.txt" in missing.fix   # optional: never a failure
    assert doctor._lister_check(ProviderConfig(ytdlp_python="other-python")).status == doctor.INFO
