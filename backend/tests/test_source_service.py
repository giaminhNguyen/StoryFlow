"""SourceService (roadmap 4.1): links / playlists / channels -> projects, the processed-video ledger, feeds and
sync. Deterministic: file SQLite from conftest, FakeVideoLister, mutable clock, no network, no sleeps."""

import dataclasses
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from storyflow.artifacts import ArtifactStore
from storyflow.errors import CapacityUnavailable, InvalidState, NotFound, ValidationFailed
from storyflow.models import ChannelWorkflow, SourceFeed, StoryProject
from storyflow.pipeline import PipelineContext
from storyflow.source_service import DEFAULT_LIMIT, MAX_LIMIT, MAX_SOURCES, SourceService, SourcesResult
from storyflow.sources import FakeVideoLister, SourceError, VideoRef

NOW = datetime(2026, 5, 1, 9, 0, 0)
CHANNEL_URL = "https://www.youtube.com/@Chan"          # canonical ref == this
OTHER_URL = "https://www.youtube.com/@Other"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLplaylist0001"
PLAYLIST_REF = "PLplaylist0001"
ADDED_KEYS = {"project_id", "video_id", "title", "slug", "feed_id"}
DUPLICATE_KEYS = {"video_id", "title", "reason", "project_id"}
FEED_KEYS = {"id", "kind", "ref", "title", "listed", "added", "known"}


def vid(i: int) -> str:
    return f"vid{i:08d}"          # 11 valid YouTube-id characters


def videos(n, start=0, duration=None):
    return [VideoRef(vid(i), f"Title {i}", duration) for i in range(start, start + n)]


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def lister():
    return FakeVideoLister({
        CHANNEL_URL: ("Chan Title", videos(30)),
        OTHER_URL: ("Other Title", videos(5, start=500)),
        PLAYLIST_REF: ("Playlist Title", videos(5, start=100)),
    })


@pytest.fixture
def make_ctx(session_factory, tmp_path, clock):
    def _make(video_lister=None):
        return PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "artifacts"),
                               subtitle_client=None, clock=clock, video_lister=video_lister, inbox_dir=None)
    return _make


@pytest.fixture
def svc(make_ctx, lister):
    return SourceService(make_ctx(lister))


def make_wf(db, status="active", name="wf"):
    wf = ChannelWorkflow(name=name, mode="auto", status=status, config={},
                         finished_at=NOW if status == "finished" else None)
    db.add(wf)
    db.commit()
    return wf


def add_project(db, workflow_id, video_id, title="T", slug=None):
    p = StoryProject(channel_workflow_id=workflow_id, title=title, slug=slug or f"slug-{video_id}", video_id=video_id)
    db.add(p)
    db.commit()
    return p


def projects(db, workflow_id):
    return db.scalars(select(StoryProject).where(StoryProject.channel_workflow_id == workflow_id)
                      .order_by(StoryProject.created_at, StoryProject.id), execution_options={"populate_existing": True}
                      ).all()


def feeds(db, workflow_id):
    return db.scalars(select(SourceFeed).where(SourceFeed.channel_workflow_id == workflow_id)
                      .order_by(SourceFeed.created_at, SourceFeed.id), execution_options={"populate_existing": True}
                      ).all()


def fresh(db, model, pk):
    return db.get(model, pk, populate_existing=True)


def state(db, workflow_id):
    """Everything a call could change: used to prove a rejected call left the database untouched."""
    wf = fresh(db, ChannelWorkflow, workflow_id)
    return (db.scalar(select(func.count()).select_from(StoryProject)),
            db.scalar(select(func.count()).select_from(SourceFeed)), wf.status, wf.updated_at, wf.finished_at)


# --- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize("sources", [[], None, "abcdefghijk", {"a": 1}, ()])
def test_sources_must_be_a_non_empty_list(db, svc, lister, sources):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, sources)
    assert exc.value.details["reason"] == "sources_required"
    assert state(db, wf.id) == before and lister.calls == []


def test_too_many_sources(db, svc):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [vid(i) for i in range(MAX_SOURCES + 1)])
    assert exc.value.details["reason"] == "too_many_sources" and exc.value.details["max"] == MAX_SOURCES
    assert state(db, wf.id) == before
    assert svc.add_sources(wf.id, [vid(i) for i in range(MAX_SOURCES)]).changed  # exactly the maximum is fine


@pytest.mark.parametrize("sources,index", [
    (["not a link"], 0), ([vid(1), "https://example.com/watch?v=abcdefghijk"], 1), ([123], 0),
    ([vid(1), vid(2), ""], 2), ([vid(1), "https://www.youtube.com/watch?v=short"], 1),
    (["inbox:../secret.txt"], 0), (["inbox:folder/x.txt"], 0),
])
def test_invalid_source_reports_its_index_and_creates_nothing(db, svc, lister, sources, index):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, sources)
    assert exc.value.details["reason"] == "invalid_source" and exc.value.details["index"] == index
    assert state(db, wf.id) == before and lister.calls == []


@pytest.mark.parametrize("limit", [0, -1, MAX_LIMIT + 1, True, False, "5", 2.5])
def test_invalid_limit(db, svc, limit):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [CHANNEL_URL], limit=limit)
    assert exc.value.details["reason"] == "invalid_limit"
    assert state(db, wf.id) == before


@pytest.mark.parametrize("languages", [[], "vi", ["a" * 17], [1], ["vi"] * 9, [""], ["  "], {"vi": 1}])
def test_invalid_languages(db, svc, languages):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [vid(1)], languages=languages)
    assert exc.value.details["reason"] == "invalid_languages"
    assert state(db, wf.id) == before


@pytest.mark.parametrize("duration", [-1, 86401, True, "5", 1.5])
def test_invalid_min_duration(db, svc, duration):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [CHANNEL_URL], min_duration_seconds=duration)
    assert exc.value.details["reason"] == "invalid_duration"
    assert state(db, wf.id) == before


@pytest.mark.parametrize("reprocess", ["yes", 1, 0, None])
def test_invalid_reprocess(db, svc, reprocess):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [vid(1)], reprocess=reprocess)
    assert exc.value.details["reason"] == "invalid_reprocess"
    assert state(db, wf.id) == before


def test_boundary_values_are_accepted(db, svc, lister):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [CHANNEL_URL], limit=1, min_duration_seconds=0, languages=["vi"], reprocess=False)
    assert len(r.added) == 1
    r = svc.add_sources(wf.id, [OTHER_URL], limit=MAX_LIMIT)
    assert len(r.added) == 5 and lister.calls[-1] == ("channel", OTHER_URL, MAX_LIMIT)


# --- single videos --------------------------------------------------------------------------------


def test_single_video_link_creates_one_project(db, svc, lister):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, ["abcdefghijk"])
    assert r.changed and r.status == "active" and r.workflow_id == wf.id
    assert r.feeds == [] and r.duplicates == [] and r.errors == [] and r.reopened is False
    (entry,) = r.added
    assert set(entry) == ADDED_KEYS
    (p,) = projects(db, wf.id)
    assert (entry["project_id"], entry["video_id"], entry["title"], entry["slug"], entry["feed_id"]) == (
        p.id, "abcdefghijk", "YouTube abcdefghijk", "youtube-abcdefghijk", None)
    assert p.video_id == "abcdefghijk" and p.title == "YouTube abcdefghijk" and p.feed_id is None
    assert p.source_config == {"kind": "video", "video_id": "abcdefghijk"}
    assert p.slug == "youtube-abcdefghijk" and p.status == "active"
    assert feeds(db, wf.id) == [] and lister.calls == []          # a plain video never touches the lister


@pytest.mark.parametrize("link", [
    "https://www.youtube.com/watch?v=abcdefghijk", "https://youtu.be/abcdefghijk",
    "https://www.youtube.com/shorts/abcdefghijk", "youtube.com/watch?v=abcdefghijk&list=PLplaylist0001",
])
def test_every_video_link_form_maps_to_the_same_video_id(db, svc, link):
    wf = make_wf(db)
    svc.add_sources(wf.id, [link])
    (p,) = projects(db, wf.id)
    assert p.video_id == "abcdefghijk"


def test_slug_collision_between_case_variants_of_a_video_id(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, ["abcdefghijk", "ABCDEFGHIJK"])           # distinct ids, identical slug base
    assert [p.slug for p in projects(db, wf.id)] == ["youtube-abcdefghijk", "youtube-abcdefghijk-2"]


def test_identical_titles_in_one_batch_get_unique_slugs(db, make_ctx):
    same = [VideoRef(vid(i), "Same title") for i in range(3)]
    svc = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("c", same)})))
    other = make_wf(db, name="other")
    add_project(db, other.id, vid(99), slug="same-title")            # a slug taken by another workflow
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL])
    assert [p.slug for p in projects(db, wf.id)] == ["same-title-2", "same-title-3", "same-title-4"]


# --- channels / playlists / feeds -----------------------------------------------------------------


def test_channel_expansion_keeps_newest_first_and_uses_default_limit(db, svc, lister, clock):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [CHANNEL_URL])
    assert DEFAULT_LIMIT == 10 and lister.calls == [("channel", CHANNEL_URL, 10)]
    rows = projects(db, wf.id)
    assert [p.video_id for p in rows] == [vid(i) for i in range(10)]
    stamps = [p.created_at for p in rows]
    assert stamps == sorted(set(stamps))                              # strictly increasing: listing order = run order
    assert [p.title for p in rows] == [f"Title {i}" for i in range(10)]
    (feed,) = feeds(db, wf.id)
    assert (feed.kind, feed.ref, feed.title, feed.limit_count, feed.known_count) == (
        "channel", CHANNEL_URL, "Chan Title", 10, 10)
    assert feed.last_scanned_at == NOW and feed.status == "active" and feed.last_error is None
    assert all(p.feed_id == feed.id for p in rows)
    assert all(p.source_config == {"kind": "video", "video_id": p.video_id} for p in rows)
    (report,) = r.feeds
    assert set(report) == FEED_KEYS
    assert report == {"id": feed.id, "kind": "channel", "ref": CHANNEL_URL, "title": "Chan Title",
                      "listed": 10, "added": 10, "known": 10}
    assert len(r.added) == 10 and set(r.added[0]) == ADDED_KEYS and r.added[0]["feed_id"] == feed.id


def test_explicit_limit_and_unlimited(db, svc, lister):
    wf = make_wf(db)
    assert len(svc.add_sources(wf.id, [CHANNEL_URL], limit=3).added) == 3 and lister.calls[-1][2] == 3
    wf2 = make_wf(db, name="wf2")
    other = FakeVideoLister({CHANNEL_URL: ("c", videos(30, start=4000))})  # not seen by wf
    svc2 = SourceService(dataclasses.replace(svc.ctx, video_lister=other))
    assert len(svc2.add_sources(wf2.id, [CHANNEL_URL], limit=None).added) == 30 and other.calls == [
        ("channel", CHANNEL_URL, None)]
    (feed,) = feeds(db, wf2.id)
    assert feed.limit_count is None


def test_second_call_reuses_the_feed_and_only_adds_new_videos(db, svc, clock):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=10)
    clock.advance(3600)
    r = svc.add_sources(wf.id, [CHANNEL_URL], limit=15)
    (feed,) = feeds(db, wf.id)                                        # upsert: still ONE feed row
    assert feed.limit_count == 15 and feed.known_count == 15 and feed.last_scanned_at == NOW + timedelta(hours=1)
    assert [a["video_id"] for a in r.added] == [vid(i) for i in range(10, 15)]
    assert [d["video_id"] for d in r.duplicates] == [vid(i) for i in range(10)]
    assert {d["reason"] for d in r.duplicates} == {"in_workflow"}
    assert all(set(d) == DUPLICATE_KEYS and d["project_id"] for d in r.duplicates)
    assert r.feeds[0]["listed"] == 15 and r.feeds[0]["added"] == 5 and r.feeds[0]["known"] == 15
    assert len(projects(db, wf.id)) == 15


def test_playlist_creates_a_playlist_feed(db, svc, lister):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [PLAYLIST_URL])
    assert lister.calls == [("playlist", PLAYLIST_REF, 10)]
    (feed,) = feeds(db, wf.id)
    assert (feed.kind, feed.ref, feed.title, feed.known_count) == ("playlist", PLAYLIST_REF, "Playlist Title", 5)
    assert [a["video_id"] for a in r.added] == [vid(i) for i in range(100, 105)]


def test_several_sources_in_one_call_share_the_call_order(db, svc):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, ["abcdefghijk", OTHER_URL, PLAYLIST_URL], limit=2)
    assert [a["video_id"] for a in r.added] == ["abcdefghijk", vid(500), vid(501), vid(100), vid(101)]
    assert [p.video_id for p in projects(db, wf.id)] == [a["video_id"] for a in r.added]
    assert [f["kind"] for f in r.feeds] == ["channel", "playlist"] and len(feeds(db, wf.id)) == 2


def test_created_at_is_strictly_increasing_across_calls_even_with_a_frozen_clock(db, svc, clock):
    """Regression: the second call stamped NOW+1us..again, interleaving with the first call's projects."""
    wf = make_wf(db)
    svc.add_sources(wf.id, [vid(1), vid(2), vid(3)])
    first = [p.id for p in projects(db, wf.id)]
    svc.add_sources(wf.id, [vid(4), vid(5), vid(6)])                   # same frozen NOW
    rows = projects(db, wf.id)
    assert [p.video_id for p in rows] == [vid(i) for i in range(1, 7)]
    assert [p.id for p in rows][:3] == first
    stamps = [p.created_at for p in rows]
    assert stamps == sorted(set(stamps))


# --- languages ------------------------------------------------------------------------------------


def test_languages_are_only_stored_when_given(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, ["abcdefghijk"])
    svc.add_sources(wf.id, ["ABCDEFGHIJK"], languages=[" vi ", "en"])
    a, b = projects(db, wf.id)
    assert a.source_config == {"kind": "video", "video_id": "abcdefghijk"}            # workflow default applies
    assert b.source_config == {"kind": "video", "video_id": "ABCDEFGHIJK", "languages": ["vi", "en"]}


def test_feed_languages_and_precedence(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=2, languages=["vi"])
    (feed,) = feeds(db, wf.id)
    assert feed.languages == ["vi"] and all(p.source_config["languages"] == ["vi"] for p in projects(db, wf.id))

    svc.add_sources(wf.id, [CHANNEL_URL], limit=4)                    # no languages: the feed's are used
    (feed,) = feeds(db, wf.id)
    assert feed.languages == ["vi"]
    assert [p.source_config.get("languages") for p in projects(db, wf.id)] == [["vi"]] * 4

    svc.add_sources(wf.id, [CHANNEL_URL], limit=6, languages=["en"])  # explicit languages win and replace the feed's
    (feed,) = feeds(db, wf.id)
    assert feed.languages == ["en"]
    assert [p.source_config["languages"] for p in projects(db, wf.id)][4:] == [["en"], ["en"]]


def test_min_duration_filters_but_keeps_unknown_durations(db, make_ctx):
    listed = [VideoRef(vid(0), "unknown", None), VideoRef(vid(1), "short", 30), VideoRef(vid(2), "long", 200),
              VideoRef(vid(3), "exact", 100)]
    svc = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("c", listed)})))
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [CHANNEL_URL], min_duration_seconds=100)
    assert [a["video_id"] for a in r.added] == [vid(0), vid(2), vid(3)]
    assert r.feeds[0]["listed"] == 3
    r0 = svc.add_sources(make_wf(db, name="w2").id, [CHANNEL_URL], min_duration_seconds=0, reprocess=True)
    assert len(r0.added) == 4                                          # 0 keeps everything (already-seen ones reprocessed)


# --- the ledger: duplicates ------------------------------------------------------------------------


def test_repeated_video_in_one_request_is_added_once(db, svc):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, ["abcdefghijk", "https://youtu.be/abcdefghijk", CHANNEL_URL], limit=2)
    assert [a["video_id"] for a in r.added] == ["abcdefghijk", vid(0), vid(1)]
    assert [(d["video_id"], d["reason"]) for d in r.duplicates] == [("abcdefghijk", "repeated")]
    assert len(projects(db, wf.id)) == 3
    r = svc.add_sources(wf.id, [vid(7), vid(7)])
    assert r.duplicates == [{"video_id": vid(7), "title": None, "reason": "repeated", "project_id": None}]
    assert len(r.added) == 1


def test_video_already_in_the_workflow_is_a_duplicate(db, svc):
    wf = make_wf(db)
    first = svc.add_sources(wf.id, ["abcdefghijk"])
    again = svc.add_sources(wf.id, ["abcdefghijk"])
    assert again.changed is False and again.added == []
    assert again.duplicates == [{"video_id": "abcdefghijk", "title": None, "reason": "in_workflow",
                                 "project_id": first.added[0]["project_id"]}]
    assert len(projects(db, wf.id)) == 1


@pytest.mark.parametrize("other_status", ["draft", "active", "paused", "finished"])
def test_video_processed_in_another_workflow_is_a_duplicate(db, svc, other_status):
    other = make_wf(db, status=other_status, name="other")
    known = add_project(db, other.id, vid(0))
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [vid(0)])
    assert r.added == [] and r.duplicates == [
        {"video_id": vid(0), "title": None, "reason": "already_processed", "project_id": known.id}]
    assert projects(db, wf.id) == []


@pytest.mark.parametrize("other_status", ["cancelled", "abandoned"])
def test_projects_of_cancelled_workflows_do_not_count(db, svc, other_status):
    other = make_wf(db, status=other_status, name="other")
    add_project(db, other.id, vid(0))
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [vid(0)])
    assert len(r.added) == 1 and r.duplicates == []


def test_orphan_projects_do_not_count(db, svc):
    add_project(db, None, vid(0))
    wf = make_wf(db)
    assert len(svc.add_sources(wf.id, [vid(0)]).added) == 1


def test_channel_expansion_reports_titles_of_duplicates(db, svc):
    other = make_wf(db, name="other")
    known = add_project(db, other.id, vid(1))
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [CHANNEL_URL], limit=3)
    assert [a["video_id"] for a in r.added] == [vid(0), vid(2)]
    assert r.duplicates == [{"video_id": vid(1), "title": "Title 1", "reason": "already_processed",
                             "project_id": known.id}]
    assert r.feeds[0]["listed"] == 3 and r.feeds[0]["added"] == 2 and r.feeds[0]["known"] == 2


def test_reprocess_adds_the_video_again(db, svc):
    other = make_wf(db, name="other")
    add_project(db, other.id, vid(0))
    wf = make_wf(db)
    svc.add_sources(wf.id, [vid(1)])
    r = svc.add_sources(wf.id, [vid(0), vid(1)], reprocess=True)
    assert [a["video_id"] for a in r.added] == [vid(0), vid(1)] and r.duplicates == []
    assert [p.video_id for p in projects(db, wf.id)] == [vid(1), vid(0), vid(1)]
    assert len({p.slug for p in projects(db, wf.id)}) == 3           # slugs stay unique


def test_reprocess_still_collapses_repeats_inside_one_request(db, svc):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [vid(1), vid(1)], reprocess=True)
    assert len(r.added) == 1 and r.duplicates[0]["reason"] == "repeated"


# --- local inbox files ---------------------------------------------------------------------------


def test_local_inbox_source(db, svc, lister):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, ["inbox:my story.txt"], languages=["vi"])
    (p,) = projects(db, wf.id)
    assert p.video_id is None and p.feed_id is None and p.title == "my story" and p.slug == "my-story"
    assert p.source_config == {"kind": "local", "file": "my story.txt"}   # no languages for a local file
    assert r.added == [{"project_id": p.id, "video_id": None, "title": "my story", "slug": "my-story",
                        "feed_id": None}]
    assert lister.calls == []


def test_local_inbox_dedupe(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, ["inbox:a.txt"])
    again = svc.add_sources(wf.id, ["inbox:a.txt", "inbox:b.srt"])
    assert [a["title"] for a in again.added] == ["b"]
    assert again.duplicates == [{"video_id": None, "title": "a", "reason": "in_workflow", "project_id": None}]
    r = svc.add_sources(wf.id, ["inbox:c.vtt", "inbox:c.vtt"])         # repeated inside one request
    assert len(r.added) == 1 and len(r.duplicates) == 1
    again = svc.add_sources(wf.id, ["inbox:a.txt"], reprocess=True)
    assert len(again.added) == 1 and [p.title for p in projects(db, wf.id)].count("a") == 2


def test_local_file_dedupe_is_per_workflow(db, svc):
    svc.add_sources(make_wf(db, name="one").id, ["inbox:a.txt"])
    two = make_wf(db, name="two")
    assert len(svc.add_sources(two.id, ["inbox:a.txt"]).added) == 1


# --- workflow status rules ------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "active", "paused"])
def test_open_workflows_accept_sources_and_keep_their_status(db, svc, status):
    wf = make_wf(db, status=status)
    r = svc.add_sources(wf.id, [vid(1)])
    assert r.changed and r.status == status and r.reopened is False
    assert fresh(db, ChannelWorkflow, wf.id).status == status


def test_finished_workflow_is_reopened_only_when_something_was_added(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, [vid(1)])
    db.execute(update(ChannelWorkflow).where(ChannelWorkflow.id == wf.id).values(status="finished", finished_at=NOW))
    db.commit()

    nothing = svc.add_sources(wf.id, [vid(1)])                          # only a duplicate
    assert nothing.changed is False and nothing.reopened is False and nothing.status == "finished"
    row = fresh(db, ChannelWorkflow, wf.id)
    assert row.status == "finished" and row.finished_at == NOW

    r = svc.add_sources(wf.id, [vid(2)])
    assert r.changed and r.reopened is True and r.status == "active"
    row = fresh(db, ChannelWorkflow, wf.id)
    assert row.status == "active" and row.finished_at is None


@pytest.mark.parametrize("status", ["cancelled", "abandoned"])
def test_closed_workflows_are_refused_untouched(db, svc, status):
    wf = make_wf(db, status=status)
    before = state(db, wf.id)
    with pytest.raises(InvalidState) as exc:
        svc.add_sources(wf.id, [vid(1)])
    assert exc.value.details["reason"] == "workflow_closed" and exc.value.details["status"] == status
    assert state(db, wf.id) == before
    with pytest.raises(InvalidState):
        svc.sync_feeds(wf.id)


def test_unknown_workflow(db, svc):
    with pytest.raises(NotFound) as exc:
        svc.add_sources("nope", [vid(1)])
    assert exc.value.details["reason"] == "workflow_not_found"
    with pytest.raises(NotFound):
        svc.sync_feeds("nope")


# --- lister availability and errors ---------------------------------------------------------------


def test_missing_lister_only_blocks_channels_and_playlists(db, make_ctx):
    svc = SourceService(make_ctx(None))
    wf = make_wf(db)
    before = state(db, wf.id)
    for source in (CHANNEL_URL, PLAYLIST_URL):
        with pytest.raises(CapacityUnavailable) as exc:
            svc.add_sources(wf.id, [source])
        assert exc.value.details["reason"] == "lister_unavailable"
    assert state(db, wf.id) == before
    assert len(svc.add_sources(wf.id, [vid(1), "inbox:x.txt"]).added) == 2   # no lister needed


@pytest.mark.parametrize("code,error,reason", [
    ("lister_failed", ValidationFailed, "source_unreadable"),
    ("lister_timeout", CapacityUnavailable, "lister_timeout"),
    ("lister_unavailable", CapacityUnavailable, "lister_unavailable"),
])
def test_lister_errors_are_mapped(db, make_ctx, code, error, reason):
    svc = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: SourceError(code, "boom")})))
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(error) as exc:
        svc.add_sources(wf.id, [CHANNEL_URL])
    assert exc.value.details["reason"] == reason and "boom" in exc.value.message
    assert state(db, wf.id) == before


def test_unknown_channel_is_unreadable(db, svc):
    wf = make_wf(db)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, ["https://www.youtube.com/@Nobody"])
    assert exc.value.details["reason"] == "source_unreadable"


def test_a_failing_later_source_creates_nothing(db, svc):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed):
        svc.add_sources(wf.id, [vid(1), CHANNEL_URL, "https://www.youtube.com/@Nobody"])
    assert state(db, wf.id) == before and projects(db, wf.id) == [] and feeds(db, wf.id) == []


# --- sync_feeds -----------------------------------------------------------------------------------


def test_sync_adds_only_new_videos_and_moves_the_cursor(db, svc, lister, clock):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=5, languages=["vi"])
    lister.store[CHANNEL_URL] = ("Chan Title", videos(2, start=900) + videos(30))   # two new uploads on top
    clock.advance(600)
    r = svc.sync_feeds(wf.id)
    assert lister.calls[-1] == ("channel", CHANNEL_URL, 5)                # the feed's own limit
    assert [a["video_id"] for a in r.added] == [vid(900), vid(901)]
    assert [d["video_id"] for d in r.duplicates] == [vid(i) for i in range(3)]
    assert {d["reason"] for d in r.duplicates} == {"in_workflow"}
    (feed,) = feeds(db, wf.id)
    assert feed.known_count == 7 and feed.last_scanned_at == NOW + timedelta(seconds=600) and feed.status == "active"
    (report,) = r.feeds
    assert report == {"id": feed.id, "kind": "channel", "ref": CHANNEL_URL, "title": "Chan Title", "listed": 5,
                      "added": 2, "known": 7}
    assert r.changed and r.errors == [] and r.reopened is False
    new = [p for p in projects(db, wf.id) if p.video_id in (vid(900), vid(901))]
    assert all(p.source_config["languages"] == ["vi"] and p.feed_id == feed.id for p in new)   # feed languages
    assert [p.video_id for p in projects(db, wf.id)][-2:] == [vid(900), vid(901)]              # run after the old ones


def test_sync_with_nothing_new_changes_nothing(db, svc, clock):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=3)
    r = svc.sync_feeds(wf.id)
    assert r.changed is False and r.added == [] and len(r.duplicates) == 3 and r.errors == []
    assert len(projects(db, wf.id)) == 3


def test_sync_without_feeds_is_an_empty_noop_even_without_a_lister(db, make_ctx):
    svc = SourceService(make_ctx(None))
    wf = make_wf(db, status="paused")
    r = svc.sync_feeds(wf.id)
    assert isinstance(r, SourcesResult)
    assert (r.workflow_id, r.status, r.changed, r.added, r.duplicates, r.feeds, r.errors, r.reopened) == (
        wf.id, "paused", False, [], [], [], [], False)


def test_sync_needs_a_lister_when_there_are_feeds(db, svc, make_ctx):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=2)
    with pytest.raises(CapacityUnavailable) as exc:
        SourceService(make_ctx(None)).sync_feeds(wf.id)
    assert exc.value.details["reason"] == "lister_unavailable"


def test_sync_isolates_a_failing_feed_and_recovers_later(db, svc, lister, clock):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL, OTHER_URL], limit=2)
    good_ref, bad_ref = CHANNEL_URL, OTHER_URL
    lister.store[good_ref] = ("Chan Title", videos(4))                     # two more on the good feed
    lister.store[bad_ref] = SourceError("lister_failed", "channel gone")
    r = svc.sync_feeds(wf.id)
    assert r.added == []                                                   # limit 2: its newest 2 are already known
    by_ref = {f.ref: f for f in feeds(db, wf.id)}
    assert by_ref[bad_ref].status == "error" and "channel gone" in by_ref[bad_ref].last_error
    assert by_ref[good_ref].status == "active"
    assert r.errors == [{"source": bad_ref, "code": "source_unreadable", "message": "channel gone"}]
    assert [f["ref"] for f in r.feeds] == [good_ref]

    lister.store[bad_ref] = ("Other Title", videos(5, start=500))          # the channel is readable again
    clock.advance(60)
    r2 = svc.sync_feeds(wf.id)
    by_ref = {f.ref: f for f in feeds(db, wf.id)}
    assert by_ref[bad_ref].status == "active" and by_ref[bad_ref].last_error is None and r2.errors == []


def test_sync_new_videos_on_the_good_feed_while_another_feed_fails(db, svc, lister):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL, OTHER_URL], limit=2)
    lister.store[CHANNEL_URL] = ("Chan Title", videos(1, start=700) + videos(30))
    lister.store[OTHER_URL] = SourceError("lister_timeout", "slow")
    r = svc.sync_feeds(wf.id)
    assert [a["video_id"] for a in r.added] == [vid(700)]
    assert r.errors == [{"source": OTHER_URL, "code": "lister_timeout", "message": "slow"}]
    assert r.changed is True and {f.ref: f.status for f in feeds(db, wf.id)}[OTHER_URL] == "error"


def test_sync_when_every_feed_fails_changes_no_projects(db, svc, lister):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=2)
    lister.store[CHANNEL_URL] = SourceError("lister_failed", "blocked")
    r = svc.sync_feeds(wf.id)
    assert r.changed is False and r.added == [] and r.feeds == [] and r.status == "active"
    assert [e["code"] for e in r.errors] == ["source_unreadable"]
    assert len(projects(db, wf.id)) == 2


def test_sync_respects_a_feed_without_limit(db, svc, lister):
    wf = make_wf(db)
    svc.add_sources(wf.id, [OTHER_URL], limit=None)
    svc.sync_feeds(wf.id)
    assert lister.calls[-1] == ("channel", OTHER_URL, None)


def test_sync_skips_videos_processed_in_other_workflows(db, svc, lister):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=2)
    other = make_wf(db, name="other")
    known = add_project(db, other.id, vid(800))
    lister.store[CHANNEL_URL] = ("Chan Title", videos(1, start=800) + videos(30))
    r = svc.sync_feeds(wf.id)
    assert r.added == [] and r.duplicates[0] == {"video_id": vid(800), "title": "Title 800",
                                                  "reason": "already_processed", "project_id": known.id}


def test_sync_reopens_a_finished_workflow_only_when_new_videos_appear(db, svc, lister):
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=2)
    db.execute(update(ChannelWorkflow).where(ChannelWorkflow.id == wf.id).values(status="finished", finished_at=NOW))
    db.commit()
    quiet = svc.sync_feeds(wf.id)
    assert quiet.changed is False and quiet.reopened is False and quiet.status == "finished"
    assert fresh(db, ChannelWorkflow, wf.id).status == "finished"
    lister.store[CHANNEL_URL] = ("Chan Title", videos(1, start=950) + videos(30))
    r = svc.sync_feeds(wf.id)
    assert r.reopened is True and r.status == "active" and [a["video_id"] for a in r.added] == [vid(950)]
    row = fresh(db, ChannelWorkflow, wf.id)
    assert row.status == "active" and row.finished_at is None


def test_sync_keeps_a_paused_workflow_paused(db, svc, lister):
    wf = make_wf(db, status="paused")
    svc.add_sources(wf.id, [CHANNEL_URL], limit=1)
    lister.store[CHANNEL_URL] = ("Chan Title", videos(1, start=960) + videos(30))
    r = svc.sync_feeds(wf.id)
    assert r.status == "paused" and r.reopened is False and len(r.added) == 1


# --- payload shapes -------------------------------------------------------------------------------


def test_result_is_a_frozen_dataclass_with_stable_fields(db, svc):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [vid(1), CHANNEL_URL], limit=1)
    assert [f.name for f in dataclasses.fields(SourcesResult)] == [
        "workflow_id", "status", "changed", "added", "duplicates", "feeds", "errors", "reopened"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.changed = False
    assert isinstance(r.changed, bool) and isinstance(r.reopened, bool)
    assert all(set(a) == ADDED_KEYS for a in r.added) and all(set(f) == FEED_KEYS for f in r.feeds)


# --- concurrency: the ledger holds under parallel writers ------------------------------------------


def run_threads(n, fn):
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001 - the test inspects what came back
            return exc

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(worker, range(n)))


@pytest.mark.parametrize("round_no", range(4))
def test_parallel_overlapping_adds_never_duplicate_a_video_in_a_workflow(db, make_ctx, round_no):
    base = 1000 * (round_no + 1)
    lister = FakeVideoLister({CHANNEL_URL: ("A", videos(10, start=base)),
                              OTHER_URL: ("B", videos(10, start=base + 5))})   # 5 videos overlap
    svc = SourceService(make_ctx(lister))
    wf = make_wf(db)
    results = run_threads(2, lambda i: svc.add_sources(wf.id, [(CHANNEL_URL, OTHER_URL)[i]]))
    assert all(isinstance(r, SourcesResult) for r in results), results
    ids = [p.video_id for p in projects(db, wf.id)]
    assert len(ids) == len(set(ids)) == 15
    assert sum(len(r.added) for r in results) == 15 and sum(len(r.duplicates) for r in results) == 5
    assert {d["reason"] for r in results for d in r.duplicates} == {"in_workflow"}
    assert len(feeds(db, wf.id)) == 2


def test_parallel_adds_in_two_workflows_keep_the_ledger_global(db, make_ctx):
    lister = FakeVideoLister({CHANNEL_URL: ("A", videos(10, start=2000))})
    svc = SourceService(make_ctx(lister))
    w1, w2 = make_wf(db, name="w1"), make_wf(db, name="w2")
    results = run_threads(2, lambda i: svc.add_sources((w1, w2)[i].id, [CHANNEL_URL]))
    assert all(isinstance(r, SourcesResult) for r in results), results
    ids = [p.video_id for wf in (w1, w2) for p in projects(db, wf.id)]
    assert sorted(ids) == sorted(vid(i) for i in range(2000, 2010))          # every video exists exactly once
    assert sum(len(r.added) for r in results) == 10
    assert sorted(d["reason"] for r in results for d in r.duplicates) == ["already_processed"] * 10


def test_parallel_sync_and_add_do_not_duplicate(db, make_ctx):
    lister = FakeVideoLister({CHANNEL_URL: ("A", videos(6, start=3000))})
    svc = SourceService(make_ctx(lister))
    wf = make_wf(db)
    svc.add_sources(wf.id, [CHANNEL_URL], limit=3)
    lister.store[CHANNEL_URL] = ("A", videos(6, start=3000))

    def work(i):
        return svc.sync_feeds(wf.id) if i == 0 else svc.add_sources(wf.id, [CHANNEL_URL], limit=6)

    results = run_threads(2, work)
    assert all(isinstance(r, SourcesResult) for r in results), results
    ids = [p.video_id for p in projects(db, wf.id)]
    assert len(ids) == len(set(ids)) == 6
