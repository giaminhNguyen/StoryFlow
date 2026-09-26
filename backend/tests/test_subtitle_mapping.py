"""Per-video upstream failures must never look like an operator fault (review R1#3), and the SourceStep details
around them: the inbox beats a pending backoff (R1#14) and a local source never inherits a workflow video (R2#13).

The worker is exercised for real (subprocess) over a FAKE upstream package whose exception CLASSES carry the same
names as youtube-transcript-api's: the worker matches by name and never imports the upstream types.
"""

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.integrations import subtitle_subprocess as sp
from storyflow.integrations.subtitle_subprocess import SubprocessSubtitleClient
from storyflow.models import ChannelWorkflow, SourceSnapshot, StoryProject
from storyflow.pipeline import PipelineContext, StepStatus
from storyflow.providers import ProviderConfig
from storyflow.sources import MAX_INBOX_BYTES
from storyflow.story_steps import SourceStep
from storyflow.subtitles import (
    BlockedByProvider, ExternalSubtitleClient, FakeSubtitleClient, LanguageUnavailable, ProviderTimeout,
    ProviderUnavailable, SubtitleFetchFailed, SubtitlesUnavailable, upstream_error_kind,
)

# Class names mirror youtube_transcript_api._errors (the base class is deliberately NOT one of the mapped names).
FAKE_UPSTREAM = textwrap.dedent('''
    class SubtitleUnavailable(Exception): pass
    class LanguageUnavailable(Exception): pass
    class BlockedByYouTube(Exception): pass

    class CouldNotRetrieveTranscript(Exception): pass
    class VideoUnavailable(CouldNotRetrieveTranscript): pass
    class VideoUnplayable(CouldNotRetrieveTranscript): pass
    class AgeRestricted(CouldNotRetrieveTranscript): pass
    class InvalidVideoId(CouldNotRetrieveTranscript): pass
    class NotTranslatable(CouldNotRetrieveTranscript): pass
    class TranslationLanguageNotAvailable(CouldNotRetrieveTranscript): pass
    class RequestBlocked(CouldNotRetrieveTranscript): pass
    class IpBlocked(RequestBlocked): pass
    class YouTubeRequestFailed(CouldNotRetrieveTranscript): pass
    class PoTokenRequired(CouldNotRetrieveTranscript): pass
    class SomethingNew(CouldNotRetrieveTranscript): pass

    class _T:
        language = "English"
        language_code = "en"
        is_generated = False
        is_translatable = True

    def _behave(video_id):
        cls = globals().get(video_id)
        if isinstance(cls, type) and issubclass(cls, Exception):
            raise cls("problem with C:\\\\secret\\\\dir\\\\file.txt")
        if video_id == "runtime": raise RuntimeError("bug in the upstream at /home/u/x/y.py")
        if video_id == "net": raise ConnectionError("connection reset")

    def available_transcripts(video_id):
        _behave(video_id)
        return []

    def fetch_selected(video_id, languages, preference, allow_translation):
        _behave(video_id)
        return _T(), False, [{"text": "hello", "start": 0.0, "duration": 1.0}]
''')


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    backend = tmp_path_factory.mktemp("upstream") / "backend"
    (backend / "app" / "services").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("")
    (backend / "app" / "services" / "__init__.py").write_text("")
    (backend / "app" / "services" / "subtitles.py").write_text(FAKE_UPSTREAM, encoding="utf-8")
    return backend


def run_worker(upstream, video_id, op="fetch"):
    request = {"backend_dir": str(upstream), "video_id": video_id}
    done = subprocess.run([sys.executable, str(sp.WORKER_PATH), op], input=json.dumps(request).encode(),
                          capture_output=True, timeout=60)
    assert done.returncode == 0
    return json.loads(done.stdout.decode("utf-8"))


WORKER_CODES = [
    ("VideoUnavailable", "video_unavailable"), ("VideoUnplayable", "video_unavailable"),
    ("AgeRestricted", "video_unavailable"), ("InvalidVideoId", "video_unavailable"),
    ("NotTranslatable", "language_unavailable"), ("TranslationLanguageNotAvailable", "language_unavailable"),
    ("RequestBlocked", "blocked"), ("IpBlocked", "blocked"),          # IpBlocked derives from RequestBlocked
    ("YouTubeRequestFailed", "blocked"), ("PoTokenRequired", "blocked"),
    ("SomethingNew", "subtitle_failed"),                              # unknown upstream class: per item
    ("runtime", "subtitle_failed"),                                   # a plain bug in the upstream: per item
    ("net", "network"),                                               # OSError family stays transient
]


@pytest.mark.parametrize("video_id,error", WORKER_CODES)
@pytest.mark.parametrize("op", ["fetch", "list"])
def test_worker_maps_upstream_exceptions_by_name(upstream, video_id, error, op):
    out = run_worker(upstream, video_id, op)
    assert out["ok"] is False and out["error"] == error
    assert "internal" != out["error"] and out["error"] != "import_error"      # never an operator-level code
    assert "secret" not in out["message"] and "/home" not in out["message"]   # paths are scrubbed


def test_worker_level_problems_keep_their_operator_codes(upstream, tmp_path):
    assert run_worker(upstream, "")["error"] == "internal"                     # missing video id
    assert run_worker(upstream, "x", op="explode")["error"] == "internal"      # unknown op
    missing = subprocess.run([sys.executable, str(sp.WORKER_PATH), "fetch"],
                             input=json.dumps({"backend_dir": str(tmp_path / "nope"), "video_id": "x"}).encode(),
                             capture_output=True, timeout=60)
    assert json.loads(missing.stdout)["error"] == "import_error"


def test_worker_still_prefers_the_upstream_mapped_exceptions(upstream):
    # the three classes the upstream module itself raises keep their dedicated codes
    assert run_worker(upstream, "BlockedByYouTube")["error"] == "blocked"
    assert run_worker(upstream, "SubtitleUnavailable")["error"] == "no_subtitle"
    assert run_worker(upstream, "LanguageUnavailable")["error"] == "language_unavailable"


CLIENT_EXCEPTIONS = [
    ("VideoUnavailable", SubtitlesUnavailable), ("AgeRestricted", SubtitlesUnavailable),
    ("NotTranslatable", LanguageUnavailable), ("IpBlocked", BlockedByProvider),
    ("PoTokenRequired", BlockedByProvider), ("SomethingNew", SubtitleFetchFailed),
    ("runtime", SubtitleFetchFailed), ("net", ProviderTimeout),
]


@pytest.mark.parametrize("video_id,exc", CLIENT_EXCEPTIONS)
def test_subprocess_client_raises_per_item_errors(upstream, video_id, exc):
    client = SubprocessSubtitleClient(ProviderConfig(subtitle_python=sys.executable,
                                                     subtitle_backend_dir=str(upstream), subtitle_timeout=30.0))
    with pytest.raises(exc) as info:
        client.fetch(video_id)
    assert not isinstance(info.value, ProviderUnavailable)
    assert "secret" not in str(info.value) and "/home" not in str(info.value)


def test_a_crashing_worker_is_still_an_operator_problem(upstream):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 3, b"", b"boom")
    client = SubprocessSubtitleClient(ProviderConfig(subtitle_backend_dir=str(upstream)), runner=runner)
    with pytest.raises(ProviderUnavailable):
        client.fetch("x")


# --- the in-process adapter follows the same rules ------------------------------------------------------------


class _Mod:
    class SubtitleUnavailable(Exception): pass
    class LanguageUnavailable(Exception): pass
    class BlockedByYouTube(Exception): pass

    def __init__(self, exc):
        self.exc = exc

    def available_transcripts(self, video_id):
        raise self.exc

    def fetch_selected(self, *a):
        raise self.exc


class VideoUnplayable(Exception): pass
class TranslationLanguageNotAvailable(Exception): pass
class RequestBlocked(Exception): pass
class IpBlocked(RequestBlocked): pass


@pytest.mark.parametrize("exc,expected", [
    (VideoUnplayable("x"), SubtitlesUnavailable), (TranslationLanguageNotAvailable("x"), LanguageUnavailable),
    (RequestBlocked("x"), BlockedByProvider), (IpBlocked("x"), BlockedByProvider),
    (ConnectionError("reset"), ProviderTimeout), (RuntimeError("bug"), SubtitleFetchFailed),
])
def test_external_client_maps_unexpected_exceptions(exc, expected):
    client = ExternalSubtitleClient(provider_builder=lambda path: _Mod(exc))
    with pytest.raises(expected):
        client.fetch("v")
    with pytest.raises(expected):
        client.list_tracks("v")


def test_upstream_error_kind_uses_the_class_hierarchy():
    Base = type("RequestBlocked", (Exception,), {})
    Sub = type("IpBlocked", (Base,), {})
    assert upstream_error_kind(Sub("x")) == "blocked" and upstream_error_kind(RuntimeError()) is None
    Both = type("VideoUnavailable", (Base,), {})       # blocked wins over the rest
    assert upstream_error_kind(Both("x")) == "blocked"


# --- SourceStep: SubtitleFetchFailed goes through the failure policy --------------------------------------------

NOW = datetime(2026, 6, 1, 8, 0, 0)
VID = "abcdefghijk"
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "hello there", "start": 0.0}]}


class Raising(FakeSubtitleClient):
    def __init__(self, exc):
        super().__init__({VID: {"tracks": [TRACK]}})
        self.exc, self.calls = exc, 0

    def fetch(self, *a, **kw):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return super().fetch(*a, **kw)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def ctx_for(session_factory, store, client, clock=None, inbox=None):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=client,
                           clock=clock or (lambda: NOW), inbox_dir=inbox)


def project_for(db, source_config=None, policy=None, video=VID):
    cfg = {"source": {"video_id": video, "languages": ["en"]}}
    if policy is not None:
        cfg["failure_policy"] = policy
    wf = ChannelWorkflow(name="w", mode="auto", status="active", config=cfg)
    db.add(wf)
    db.commit()
    p = StoryProject(title="P", channel_workflow_id=wf.id, source_config=source_config)
    db.add(p)
    db.commit()
    return p


def fresh(db, pk):
    return db.get(StoryProject, pk, populate_existing=True)


def test_an_odd_video_is_a_per_item_failure_not_an_operator_fault(db, session_factory, store):
    client = Raising(SubtitleFetchFailed("weird"))
    project = project_for(db, policy={"on_permanent_error": "continue"})
    view = SourceStep().run(ctx_for(session_factory, store, client), project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "subtitle_failed")
    p = fresh(db, project.id)
    assert (p.status, p.status_reason) == ("needs_attention", "subtitle_failed")     # only this project ends


def test_an_odd_video_pauses_by_default_like_any_permanent_error(db, session_factory, store):
    project = project_for(db)
    view = SourceStep().run(ctx_for(session_factory, store, Raising(SubtitleFetchFailed("weird"))), project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "subtitle_failed")
    assert fresh(db, project.id).status == "active"


def test_skip_policy_does_not_swallow_a_subtitle_failed_video(db, session_factory, store):
    project = project_for(db, policy={"on_no_subtitle": "skip"})       # skip is for "no subtitle", not for errors
    SourceStep().run(ctx_for(session_factory, store, Raising(SubtitleFetchFailed("weird"))), project.id)
    assert fresh(db, project.id).status == "active"


# --- inbox beats a pending backoff (R1#14) ---------------------------------------------------------------------

BACKOFF = {"subtitle_retries": 5, "retry_base_seconds": 600, "retry_max_seconds": 900}


def test_a_dropped_inbox_file_is_used_inside_the_backoff_window(db, session_factory, store, inbox):
    clock = Clock()
    client = Raising(BlockedByProvider("429"))
    ctx = ctx_for(session_factory, store, client, clock, inbox)
    project = project_for(db, policy=BACKOFF)

    assert SourceStep().run(ctx, project.id).error_code == "provider_blocked"
    assert fresh(db, project.id).next_attempt_at == NOW + timedelta(seconds=600)
    clock.now += timedelta(seconds=30)                                  # still 9.5 minutes of backoff left
    assert SourceStep().run(ctx, project.id).status is StepStatus.NOT_STARTED and client.calls == 1

    (inbox / f"{VID}.txt").write_text("Lời do tôi gõ tay.", encoding="utf-8")
    view = SourceStep().run(ctx, project.id)
    assert view.status is StepStatus.COMPLETED and client.calls == 1     # provider untouched, no waiting
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project.id))
    assert snap.meta["provider"] == "InboxFile" and snap.content == "Lời do tôi gõ tay."
    p = fresh(db, project.id)
    assert p.source_attempts == 0 and p.next_attempt_at is None          # the backoff state is cleared on success


def test_without_an_inbox_file_the_backoff_still_protects_the_provider(db, session_factory, store, inbox):
    clock = Clock()
    client = Raising(BlockedByProvider("429"))
    ctx = ctx_for(session_factory, store, client, clock, inbox)
    project = project_for(db, policy=BACKOFF)
    SourceStep().run(ctx, project.id)
    for _ in range(3):
        clock.now += timedelta(seconds=60)
        assert SourceStep().run(ctx, project.id).status is StepStatus.NOT_STARTED
    assert client.calls == 1


def test_an_unreadable_inbox_file_does_not_bypass_the_backoff(db, session_factory, store, inbox):
    clock = Clock()
    client = Raising(BlockedByProvider("429"))
    ctx = ctx_for(session_factory, store, client, clock, inbox)
    project = project_for(db, policy=BACKOFF)
    SourceStep().run(ctx, project.id)
    (inbox / f"{VID}.txt").write_bytes(b"a" * (MAX_INBOX_BYTES + 1))    # too big to be read: as good as absent
    clock.now += timedelta(seconds=60)
    assert SourceStep().run(ctx, project.id).status is StepStatus.NOT_STARTED and client.calls == 1


# --- a local source never inherits the workflow's video (R2#13) --------------------------------------------------


def test_local_source_does_not_inherit_the_workflow_video_id(db, session_factory, store, inbox):
    (inbox / "story.txt").write_text("một dòng\nhai dòng", encoding="utf-8")
    project = project_for(db, {"kind": "local", "file": "story.txt"}, video="someVideo01")   # workflow HAS a video
    view = SourceStep().run(ctx_for(session_factory, store, Raising(None), inbox=inbox), project.id)
    assert view.status is StepStatus.COMPLETED
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project.id))
    assert snap.meta["video_id"] is None and snap.meta["inbox_file"] == "story.txt"


def test_video_projects_still_record_their_video_id(db, session_factory, store):
    project = project_for(db)
    SourceStep().run(ctx_for(session_factory, store, Raising(None)), project.id)
    snap = db.scalar(select(SourceSnapshot).where(SourceSnapshot.story_project_id == project.id))
    assert snap.meta["video_id"] == VID and snap.meta["provider"] == "Raising"
