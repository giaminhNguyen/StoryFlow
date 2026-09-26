"""Ingestion hardening (review round): bounded work per call, the JSON lister protocol, concurrency / deadline
limits on yt-dlp, inbox validation + encodings, the feed's stored duration filter, the widened sync window and the
pure helpers of scripts/configure_providers.py. Deterministic: fakes, a mutable clock, no network."""

import importlib.util
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import event

from storyflow import source_service
from storyflow.errors import CapacityUnavailable, ValidationFailed
from storyflow.models import StoryProject
from storyflow.source_service import (
    MAX_LIMIT, MAX_NEW_PER_CALL, SourceService, _sync_window,
)
from storyflow.sources import (
    LISTER_MAX_CONCURRENT, ListedVideos, FakeVideoLister, ParsedSource, SourceError, VideoRef, YtDlpLister,
    decode_inbox_bytes, parse_lister_output, parse_source, read_inbox_text,
)

from test_source_service import (  # noqa: F401  (fixtures + helpers of the service tests)
    CHANNEL_URL, OTHER_URL, PLAYLIST_REF, PLAYLIST_URL, clock, feeds, fresh, inbox, lister, make_ctx, make_wf, projects,
    state, svc, vid, videos,
)

CHANNEL = ParsedSource("channel", CHANNEL_URL)
VID = "NQyV_6XXyMg"


def js(video_id=VID, duration=100, playlist="Chan - Videos", title="A title"):
    return json.dumps({"id": video_id, "duration": duration, "playlist_title": playlist, "title": title})


class Run:
    """Stands in for subprocess.run."""

    def __init__(self, stdout="", stderr="", returncode=0, hook=None):
        self.stdout, self.stderr, self.returncode, self.hook = stdout, stderr, returncode, hook
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.hook:
            self.hook()
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout.encode("utf-8"),
                                           self.stderr.encode("utf-8"))


# --- the yt-dlp command line and its output --------------------------------------------------------


def test_argv_ignores_the_users_yt_dlp_config_and_never_touches_a_cache():
    argv = YtDlpLister(python="py").argv(CHANNEL, 7)
    for flag in ("--ignore-config", "--no-cache-dir", "--flat-playlist"):
        assert flag in argv
    assert argv[argv.index("--socket-timeout") + 1] == "20" and argv[argv.index("--extractor-retries") + 1] == "2"
    assert argv[argv.index("--print") + 1] == "%(.{id,duration,playlist_title,title})j"
    assert argv[-1] == CHANNEL_URL + "/videos" and argv[argv.index("--playlist-end") + 1] == "7"


def test_json_rows_are_parsed():
    listed = parse_lister_output("\n".join([js(VID, 1464, "Giao Giao Audio - Videos", "Tiếng Việt"),
                                            js("wKTTzhA648g", None, None, None)]) + "\n")
    assert listed.title == "Giao Giao Audio" and listed.partial is False
    assert listed.videos == [VideoRef(VID, "Tiếng Việt", 1464), VideoRef("wKTTzhA648g", None, None)]


def test_a_title_can_never_forge_a_second_row():
    forged = "x\nNQyV_6XXyMg\t5\tNA\tforged title"
    listed = parse_lister_output(js("wKTTzhA648g", 10, "P", forged) + "\n")
    assert [v.video_id for v in listed.videos] == ["wKTTzhA648g"]
    assert listed.videos[0].title == forged                    # the title keeps its newline, nothing was split off


def test_bad_json_rows_and_non_objects_are_ignored_but_tab_rows_still_work():
    tab = "\t".join(["3y9gpIzsIAE", "1997", "Chan - Videos", "Old format"])
    listed = parse_lister_output("\n".join(["{not json", "[1, 2]", '"text"', "null", js(VID), tab]))
    assert [v.video_id for v in listed.videos] == [VID, "3y9gpIzsIAE"]


def test_json_rows_skip_private_deleted_invalid_and_repeated_videos():
    out = "\n".join([js("AAAAAAAAAAA", title="[Private video]"), js("BBBBBBBBBBB", title="[Deleted video]"),
                     js("bad", title="x"), js(VID), js(VID, title="again")])
    assert [v.video_id for v in parse_lister_output(out).videos] == [VID]


@pytest.mark.parametrize("duration,expected", [(120, 120), (12.9, 12), (None, None), ("NA", None), (-5, None),
                                               (float("inf"), None), ("abc", None)])
def test_json_durations(duration, expected):
    assert parse_lister_output(js(duration=duration)).videos[0].duration_seconds == expected


def test_a_run_that_ends_with_errors_after_some_videos_is_flagged_partial():
    run = Run(stdout=js(VID) + "\n", stderr="ERROR: HTTP Error 429", returncode=1)
    listed = YtDlpLister(python="py", runner=run).list_videos(CHANNEL, 5)
    assert [v.video_id for v in listed.videos] == [VID] and listed.partial is True


def test_a_clean_run_is_not_partial_and_a_failed_empty_run_still_raises():
    ok = YtDlpLister(python="py", runner=Run(stdout=js(VID))).list_videos(CHANNEL, 5)
    assert ok.partial is False
    with pytest.raises(SourceError) as exc:
        YtDlpLister(python="py", runner=Run(stderr="ERROR: not found", returncode=1)).list_videos(CHANNEL, 5)
    assert exc.value.code == "lister_failed"


def test_the_per_call_timeout_can_only_shorten_the_listers_own():
    run = Run(stdout=js(VID))
    lister = YtDlpLister(python="py", timeout=120.0, runner=run)
    lister.list_videos(CHANNEL, 5, timeout=7.0)
    lister.list_videos(CHANNEL, 5, timeout=9999.0)
    lister.list_videos(CHANNEL, 5)
    short, capped, default = (kw["timeout"] for _c, kw in run.calls)
    assert short <= 7.0 and 6.0 < short and capped <= 120.0 and 119.0 < capped and default <= 120.0


def test_at_most_two_listings_run_at_the_same_time():
    lock, state_ = threading.Lock(), {"now": 0, "max": 0}
    release = threading.Event()

    def hook():
        with lock:
            state_["now"] += 1
            state_["max"] = max(state_["max"], state_["now"])
        release.wait(5)
        with lock:
            state_["now"] -= 1

    lister = YtDlpLister(python="py", runner=Run(stdout=js(VID), hook=hook))
    threads = [threading.Thread(target=lister.list_videos, args=(CHANNEL, 5)) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.4)                                   # let every thread reach the semaphore
    assert state_["now"] == LISTER_MAX_CONCURRENT == 2
    release.set()
    for t in threads:
        t.join(10)
    assert state_["max"] == 2 and state_["now"] == 0


def test_waiting_for_a_busy_lister_times_out_cleanly_and_frees_nothing_it_never_took():
    release = threading.Event()
    started = threading.Semaphore(0)

    def hook():
        started.release()
        release.wait(5)

    busy = YtDlpLister(python="py", runner=Run(stdout=js(VID), hook=hook))
    holders = [threading.Thread(target=busy.list_videos, args=(CHANNEL, 5)) for _ in range(2)]
    for t in holders:
        t.start()
    assert started.acquire(timeout=5) and started.acquire(timeout=5)
    try:
        with pytest.raises(SourceError) as exc:
            YtDlpLister(python="py", runner=Run(stdout=js(VID))).list_videos(CHANNEL, 5, timeout=0.2)
        assert exc.value.code == "lister_timeout"
    finally:
        release.set()
        for t in holders:
            t.join(10)
    # both slots are back: two more listings can start at once
    assert YtDlpLister(python="py", runner=Run(stdout=js(VID))).list_videos(CHANNEL, 5).videos


def test_the_slot_is_released_when_the_process_fails_or_times_out():
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    for _ in range(LISTER_MAX_CONCURRENT + 2):        # more failures than slots: a leak would deadlock here
        with pytest.raises(SourceError):
            YtDlpLister(python="py", runner=boom).list_videos(CHANNEL, 5, timeout=1)


# --- source parsing -----------------------------------------------------------------------------


def test_percent_encoded_handles_are_decoded():
    assert parse_source("https://www.youtube.com/@%E6%97%A5%E6%9C%AC").ref == "https://www.youtube.com/@日本"
    assert parse_source("https://www.youtube.com/@%E6%97%A5%E6%9C%AC/videos").ref == "https://www.youtube.com/@日本"
    assert parse_source("youtube.com/c/Ch%C3%A0o").ref == "https://www.youtube.com/c/Chào"


@pytest.mark.parametrize("text", ["@..", "@.", "@...", "https://www.youtube.com/@..", "https://www.youtube.com/c/..",
                                  "https://www.youtube.com/user/...", "https://www.youtube.com/c/%2E%2E",
                                  "https://www.youtube.com/@%2E%2E"])
def test_names_made_only_of_dots_are_rejected(text):
    with pytest.raises(SourceError) as exc:
        parse_source(text)
    assert exc.value.code == "invalid_source"


def test_an_encoded_slash_cannot_smuggle_a_path_into_the_channel_url():
    with pytest.raises(SourceError):
        parse_source("https://www.youtube.com/@a%2Fb")


# --- inbox: encodings ----------------------------------------------------------------------------

VI = "Xin chào, đây là truyện tiếng Việt: đặc biệt, kịch tính!"


@pytest.mark.parametrize("raw", [
    VI.encode("utf-8"), VI.encode("utf-8-sig"), VI.encode("utf-16"),                     # Notepad "Unicode" (BOM + LE)
    b"\xfe\xff" + VI.encode("utf-16-be"), b"\xff\xfe" + VI.encode("utf-16-le"),
])
def test_unicode_files_are_decoded(raw):
    assert decode_inbox_bytes(raw) == VI


def test_old_ansi_files_fall_back_to_cp1252():
    assert decode_inbox_bytes("café “quoted” – ok".encode("cp1252")) == "café “quoted” – ok"


@pytest.mark.parametrize("raw", [b"\x00\x01\x02\x03", b"abc\x00def", b"\x81\x8d\x8f\x90", b"\x01\x02\x03\x04\x05" * 10])
def test_binary_and_undecodable_bytes_are_refused(raw):
    assert decode_inbox_bytes(raw) is None


def test_read_inbox_text_uses_the_decoder(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "vi.txt").write_bytes(VI.encode("utf-16"))
    assert read_inbox_text(folder, "vi.txt") == VI
    (folder / "sub.srt").write_bytes("1\n00:00:01,000 --> 00:00:02,000\n".encode("utf-16") + VI.encode("utf-16")[2:])
    assert VI in read_inbox_text(folder, "sub.srt")


# --- bounded work per call -----------------------------------------------------------------------


def big_channel(n, start=0):
    return [VideoRef(f"v{start + i:010d}", f"Big video {start + i}", 600) for i in range(n)]


@pytest.fixture
def big():
    return FakeVideoLister({"https://www.youtube.com/@A": ("A", big_channel(600)),
                            "https://www.youtube.com/@B": ("B", big_channel(600, start=600))})


def test_one_call_creates_at_most_max_new_per_call_and_says_so(db, make_ctx, big):
    service = SourceService(make_ctx(big))
    wf = make_wf(db)
    r = service.add_sources(wf.id, ["https://www.youtube.com/@A", "https://www.youtube.com/@B"], limit=MAX_LIMIT)
    assert MAX_NEW_PER_CALL == 1000
    assert len(r.added) == 1000 and r.truncated is True and r.not_added == 200 and r.changed is True
    assert len(projects(db, wf.id)) == 1000
    assert [f["added"] for f in r.feeds] == [600, 400]                    # the second feed was cut, not the first

    rest = service.add_sources(wf.id, ["https://www.youtube.com/@A", "https://www.youtube.com/@B"], limit=MAX_LIMIT)
    assert len(rest.added) == 200 and rest.truncated is False and rest.not_added == 0     # the ledger finishes the job
    assert rest.duplicates_count == 1000
    assert len(projects(db, wf.id)) == 1200


def test_the_duplicate_list_is_capped_but_counted(db, make_ctx, big):
    service = SourceService(make_ctx(big))
    wf = make_wf(db)
    refs_ = ["https://www.youtube.com/@A", "https://www.youtube.com/@B"]
    service.add_sources(wf.id, refs_, limit=MAX_LIMIT)
    service.add_sources(wf.id, refs_, limit=MAX_LIMIT)          # now every one of the 1200 videos is known
    again = service.add_sources(wf.id, refs_, limit=MAX_LIMIT)
    assert again.changed is False and again.duplicates_count == 1200 and len(again.duplicates) == 1000


def test_a_thousand_projects_are_created_quickly_without_per_insert_like_scans(db, make_ctx, session_factory):
    lister_ = FakeVideoLister({"https://www.youtube.com/@Big": ("Big", big_channel(1000))})
    service = SourceService(make_ctx(lister_))
    wf = make_wf(db)
    engine = session_factory.kw["bind"]
    statements = []

    def record(conn, cursor, statement, *a, **k):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        started = time.perf_counter()
        r = service.add_sources(wf.id, ["https://www.youtube.com/@Big"], limit=MAX_LIMIT)
        elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(r.added) == 1000 and elapsed < 10
    assert not [s for s in statements if " LIKE " in s.upper()]           # slugs are allocated from memory
    assert len(statements) < 200                                          # ~1 per project would be 1000+


def test_identical_titles_get_distinct_slugs_even_next_to_existing_ones(db, make_ctx):
    same = [VideoRef(f"s{i:010d}", "Same title", 60) for i in range(30)]
    service = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("C", same)})))
    wf = make_wf(db)
    db.add(StoryProject(channel_workflow_id=wf.id, title="x", slug="same-title", video_id=None))
    db.add(StoryProject(channel_workflow_id=wf.id, title="x", slug="same-title-2", video_id=None))
    db.commit()
    service.add_sources(wf.id, [CHANNEL_URL], limit=30)
    slugs = [p.slug for p in projects(db, wf.id) if p.video_id]
    assert len(slugs) == len(set(slugs)) == 30 and "same-title" not in slugs and "same-title-2" not in slugs


def test_titles_with_newlines_and_runs_of_spaces_are_normalised(db, make_ctx):
    rows = [VideoRef("n0000000001", "Line one\nline   two\t!", 60), VideoRef("n0000000002", "\n\n", 60)]
    service = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("C", rows)})))
    wf = make_wf(db)
    service.add_sources(wf.id, [CHANNEL_URL], limit=5)
    assert [p.title for p in projects(db, wf.id)] == ["Line one line two !", "YouTube n0000000002"]


# --- the feed remembers its filters; the re-scan window ------------------------------------------


def test_min_duration_is_stored_on_the_feed_and_applied_by_sync(db, make_ctx):
    mixed = [VideoRef(vid(i), f"V{i}", 100 if i % 2 else 900) for i in range(10)]     # odd ones are short
    store = {CHANNEL_URL: ("C", mixed)}
    service = SourceService(make_ctx(FakeVideoLister(store)))
    wf = make_wf(db)
    r = service.add_sources(wf.id, [CHANNEL_URL], limit=10, min_duration_seconds=300)
    assert [a["video_id"] for a in r.added] == [vid(i) for i in range(0, 10, 2)]
    (feed,) = feeds(db, wf.id)
    assert feed.min_duration_seconds == 300
    store[CHANNEL_URL] = ("C", [VideoRef(vid(20), "New short", 90), VideoRef(vid(21), "New long", 1200)] + mixed)
    s = service.sync_feeds(wf.id)
    assert [a["video_id"] for a in s.added] == [vid(21)]                 # the short upload stays filtered out
    assert len(projects(db, wf.id)) == 6


def test_adding_the_feed_again_without_a_filter_clears_it(db, make_ctx):
    service = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("C", videos(3, duration=50))})))
    wf = make_wf(db)
    service.add_sources(wf.id, [CHANNEL_URL], limit=3, min_duration_seconds=300)
    assert feeds(db, wf.id)[0].min_duration_seconds == 300 and projects(db, wf.id) == []
    r = service.add_sources(wf.id, [CHANNEL_URL], limit=3)
    assert len(r.added) == 3 and feeds(db, wf.id)[0].min_duration_seconds is None


@pytest.mark.parametrize("limit,window", [(1, 50), (10, 50), (16, 50), (17, 51), (100, 300), (334, 1000), (1000, 1000),
                                          (None, 1000)])
def test_sync_window_formula(limit, window):
    assert _sync_window(limit) == window


def test_sync_lists_the_widened_window_and_finds_videos_the_first_add_never_saw(db, make_ctx):
    store = {CHANNEL_URL: ("C", videos(60))}
    fake = FakeVideoLister(store)
    service = SourceService(make_ctx(fake))
    wf = make_wf(db)
    first = service.add_sources(wf.id, [CHANNEL_URL], limit=10)
    assert len(first.added) == 10 and first.feeds[0]["window_full"] is True
    r = service.sync_feeds(wf.id)
    assert fake.calls[-1] == ("channel", CHANNEL_URL, 50)
    assert len(r.added) == 40 and r.feeds[0]["window_full"] is True        # 50 listed of 60: still more beyond
    assert r.duplicates_count == 10


def test_window_full_is_false_when_the_channel_is_smaller_than_the_request(db, make_ctx):
    service = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("C", videos(4))})))
    wf = make_wf(db)
    (report,) = service.add_sources(wf.id, [CHANNEL_URL], limit=10).feeds
    assert report["window_full"] is False and report["listed"] == 4


def test_window_full_counts_the_listing_before_the_duration_filter(db, make_ctx):
    short = [VideoRef(vid(i), f"S{i}", 10) for i in range(5)]
    service = SourceService(make_ctx(FakeVideoLister({CHANNEL_URL: ("C", short)})))
    wf = make_wf(db)
    (report,) = service.add_sources(wf.id, [CHANNEL_URL], limit=5, min_duration_seconds=300).feeds
    assert report["listed"] == 0 and report["added"] == 0 and report["window_full"] is True   # 5 asked, 5 returned


def test_a_playlist_reports_playlist_order_and_a_channel_newest_first(db, svc):
    wf = make_wf(db)
    r = svc.add_sources(wf.id, [PLAYLIST_URL, CHANNEL_URL], limit=3)
    assert {f["kind"]: f["order"] for f in r.feeds} == {"playlist": "playlist_order", "channel": "newest_first"}


class PartialLister(FakeVideoLister):
    def list_videos(self, source, limit, timeout=None):
        listed = super().list_videos(source, limit, timeout)
        return ListedVideos(listed.title, listed.videos, partial=True)


def test_a_partial_listing_is_a_warning_not_an_exception(db, make_ctx):
    service = SourceService(make_ctx(PartialLister({CHANNEL_URL: ("C", videos(3))})))
    wf = make_wf(db)
    r = service.add_sources(wf.id, [CHANNEL_URL], limit=5)
    assert len(r.added) == 3 and r.feeds[0]["partial"] is True
    assert [(w["source"], w["code"]) for w in r.warnings] == [(CHANNEL_URL, "partial_listing")]
    s = service.sync_feeds(wf.id)
    assert [w["code"] for w in s.warnings] == ["partial_listing"] and s.errors == []


def test_a_clean_listing_has_no_warnings(db, svc):
    r = svc.add_sources(make_wf(db).id, [CHANNEL_URL], limit=3)
    assert r.warnings == [] and r.feeds[0]["partial"] is False


# --- deadline, source index -----------------------------------------------------------------------


def test_every_listing_gets_the_remaining_time_budget(db, svc, lister):
    svc.add_sources(make_wf(db).id, [CHANNEL_URL, OTHER_URL], limit=3)
    first, second = lister.timeouts
    assert 290 < second <= first <= source_service.LIST_DEADLINE_SECONDS


class SlowLister(FakeVideoLister):
    def list_videos(self, source, limit, timeout=None):
        time.sleep(0.15)
        return super().list_videos(source, limit, timeout)


def test_a_call_that_runs_out_of_time_stops_with_the_index_and_creates_nothing(db, make_ctx, monkeypatch):
    monkeypatch.setattr(source_service, "LIST_DEADLINE_SECONDS", 0.1)
    service = SourceService(make_ctx(SlowLister({CHANNEL_URL: ("C", videos(3)), OTHER_URL: ("O", videos(3, 50))})))
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(CapacityUnavailable) as exc:
        service.add_sources(wf.id, [vid(1), CHANNEL_URL, OTHER_URL], limit=3)
    assert exc.value.details["reason"] == "lister_timeout" and exc.value.details["index"] == 2
    assert state(db, wf.id) == before and projects(db, wf.id) == [] and feeds(db, wf.id) == []


@pytest.mark.parametrize("bad_at", [0, 1, 2])
def test_an_unreadable_source_reports_its_index(db, make_ctx, bad_at):
    urls = [CHANNEL_URL, OTHER_URL, PLAYLIST_URL]
    store = {CHANNEL_URL: ("C", videos(2)), OTHER_URL: ("O", videos(2, 20)), PLAYLIST_REF: ("P", videos(2, 40))}
    ref = [CHANNEL_URL, OTHER_URL, PLAYLIST_REF][bad_at]
    store[ref] = SourceError("lister_failed", "gone")
    service = SourceService(make_ctx(FakeVideoLister(store)))
    wf = make_wf(db)
    with pytest.raises(ValidationFailed) as exc:
        service.add_sources(wf.id, urls, limit=2)
    assert exc.value.details == {"reason": "source_unreadable", "index": bad_at}
    assert projects(db, wf.id) == []


def test_a_lister_without_a_timeout_parameter_still_works(db, make_ctx):
    class Old:                                     # a custom lister written before the timeout argument existed
        def list_videos(self, source, limit):
            return ListedVideos("Old", videos(2))

    r = SourceService(make_ctx(Old())).add_sources(make_wf(db).id, [CHANNEL_URL], limit=2)
    assert len(r.added) == 2


# --- inbox sources are validated when they are added ---------------------------------------------


def test_a_missing_inbox_file_is_rejected_up_front_with_its_index(db, svc):
    wf = make_wf(db)
    before = state(db, wf.id)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, [vid(1), "inbox:a.txt", "inbox:typo.txt"])
    assert exc.value.details == {"reason": "inbox_file_missing", "index": 2}
    assert state(db, wf.id) == before and projects(db, wf.id) == []       # not even the valid ones


@pytest.mark.parametrize("payload", [b"", b"   \r\n\t ", b"\x00\x01\x02 binary", b"\x81\x8d\x8f\x90"])
def test_an_unreadable_or_empty_inbox_file_is_rejected(db, svc, inbox, payload):
    (inbox / "bad.txt").write_bytes(payload)
    wf = make_wf(db)
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(wf.id, ["inbox:bad.txt"])
    assert exc.value.details == {"reason": "inbox_file_unreadable", "index": 0}
    assert projects(db, wf.id) == []


def test_an_oversized_inbox_file_is_unreadable(db, svc, inbox):
    (inbox / "huge.txt").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ValidationFailed) as exc:
        svc.add_sources(make_wf(db).id, ["inbox:huge.txt"])
    assert exc.value.details["reason"] == "inbox_file_unreadable"


def test_windows_saved_files_are_accepted(db, svc, inbox):
    (inbox / "utf16.txt").write_bytes(VI.encode("utf-16"))
    (inbox / "ansi.srt").write_bytes("1\r\n00:00:01,000 --> 00:00:02,000\r\ncafé\r\n".encode("cp1252"))
    wf = make_wf(db)
    assert len(svc.add_sources(wf.id, ["inbox:utf16.txt", "inbox:ansi.srt"]).added) == 2


def test_the_inbox_folder_is_created_on_first_use(db, make_ctx, tmp_path):
    ctx = make_ctx(None)
    ctx.inbox_dir = tmp_path / "fresh" / "inbox"
    assert not ctx.inbox_dir.exists()
    with pytest.raises(ValidationFailed) as exc:
        SourceService(ctx).add_sources(make_wf(db).id, ["inbox:a.txt"])
    assert exc.value.details["reason"] == "inbox_file_missing" and ctx.inbox_dir.is_dir()
    (ctx.inbox_dir / "a.txt").write_text("hello there", encoding="utf-8")            # now the user can drop files
    assert len(SourceService(ctx).add_sources(make_wf(db, name="w2").id, ["inbox:a.txt"]).added) == 1


def test_no_inbox_configured_means_no_local_files(db, make_ctx):
    ctx = make_ctx(None)
    ctx.inbox_dir = None
    with pytest.raises(ValidationFailed) as exc:
        SourceService(ctx).add_sources(make_wf(db).id, ["inbox:a.txt"])
    assert exc.value.details["reason"] == "inbox_file_missing"


@pytest.mark.skipif(os.name != "nt", reason="file names are case-insensitive only on Windows")
def test_local_file_names_are_compared_case_insensitively(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, ["inbox:a.txt"])
    again = svc.add_sources(wf.id, ["inbox:A.TXT"])
    assert again.added == [] and again.duplicates[0]["reason"] == "in_workflow"


# --- the slim workflow view -----------------------------------------------------------------------


def test_workflow_brief_counts_the_durable_project_statuses(db, svc):
    wf = make_wf(db)
    svc.add_sources(wf.id, [vid(1), vid(2), vid(3)])
    rows = projects(db, wf.id)
    rows[0].status, rows[1].status = "completed", "skipped"
    db.commit()
    brief = svc.workflow_brief(wf.id)
    assert brief == {"id": wf.id, "name": "wf", "status": "active", "status_reason": None, "project_count": 3,
                     "counts": {"active": 1, "completed": 1, "skipped": 1}}


# --- scripts/configure_providers.py (pure helpers) -------------------------------------------------


@pytest.fixture(scope="module")
def cfg():
    path = Path(__file__).resolve().parents[2] / "scripts" / "configure_providers.py"
    spec = importlib.util.spec_from_file_location("configure_providers_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_ENV = ("# my notes\r\nSTORYFLOW_YTDLP_PYTHON=C:/tools/py.exe\r\nSTORYFLOW_STORY_RUNNER=claude-cli\r\n"
           "STORYFLOW_VIENEU_ROOT=C:/tts\r\nSTORYFLOW_LISTER_TIMEOUT=45\r\nMY_OWN=1\r\nSTORYFLOW_INBOX_DIR=D:/subs\r\n")


def test_merge_keeps_unknown_keys_comments_and_order_and_drops_stale_managed_keys(cfg):
    out = cfg.merge_env_text(OLD_ENV, {"STORYFLOW_STORY_RUNNER": "none", "STORYFLOW_TTS_ENGINE": "none"})
    lines = out.splitlines()
    assert lines[:5] == ["# my notes", "STORYFLOW_YTDLP_PYTHON=C:/tools/py.exe", "STORYFLOW_LISTER_TIMEOUT=45",
                         "MY_OWN=1", "STORYFLOW_INBOX_DIR=D:/subs"]
    assert "STORYFLOW_VIENEU_ROOT=C:/tts" not in out                       # managed and no longer set: removed
    assert lines[-2:] == ["STORYFLOW_STORY_RUNNER=none", "STORYFLOW_TTS_ENGINE=none"]
    assert out.endswith("\n") and "\r" not in out


def test_merge_is_idempotent_and_never_duplicates_the_header(cfg):
    values = {"STORYFLOW_STORY_RUNNER": "claude-cli", "STORYFLOW_STORY_TIMEOUT": "1800"}
    once = cfg.merge_env_text(OLD_ENV, values)
    twice = cfg.merge_env_text(once, values)
    assert once == twice and once.count(cfg.ENV_HEADER) == 1


def test_merge_into_nothing_and_only_managed_keys_are_written(cfg):
    out = cfg.merge_env_text("", {"STORYFLOW_STORY_RUNNER": "none", "SOMETHING_ELSE": "x"})
    assert out == cfg.ENV_HEADER + "\nSTORYFLOW_STORY_RUNNER=none\n"        # a key this script does not own is not written


def test_env_key(cfg):
    assert cfg.env_key("A=1") == "A" and cfg.env_key("  A = 1 ") == "A"
    assert cfg.env_key("# A=1") is None and cfg.env_key("") is None and cfg.env_key("nothing") is None


def test_backups_are_timestamped_and_never_overwrite_an_older_one(cfg, tmp_path):
    env = tmp_path / ".env"
    env.write_text("x", encoding="utf-8")
    when = datetime(2026, 9, 26, 10, 11, 12)
    first = cfg.backup_path(env, when)
    assert first.name == ".env.bak-20260926101112"
    first.write_text("old", encoding="utf-8")
    second = cfg.backup_path(env, when)
    assert second.name == ".env.bak-20260926101112-2" and second != first
    second.write_text("older", encoding="utf-8")
    assert cfg.backup_path(env, when).name == ".env.bak-20260926101112-3"
    assert first.read_text(encoding="utf-8") == "old"


def test_write_env_merges_into_the_real_file_and_keeps_a_backup(cfg, tmp_path, monkeypatch):
    env = tmp_path / "backend" / ".env"
    env.parent.mkdir()
    env.write_text(OLD_ENV, encoding="utf-8", newline="")
    monkeypatch.setattr(cfg, "ENV_FILE", env)
    cfg.write_env({"STORYFLOW_STORY_RUNNER": "claude-cli"})
    text = env.read_text(encoding="utf-8")
    assert "STORYFLOW_YTDLP_PYTHON=C:/tools/py.exe" in text and "STORYFLOW_STORY_RUNNER=claude-cli" in text
    (backup,) = [p for p in env.parent.iterdir() if p.name.startswith(".env.bak-")]
    assert backup.read_bytes().decode("utf-8") == OLD_ENV                   # the original, byte for byte
    cfg.write_env({"STORYFLOW_STORY_RUNNER": "none"})
    assert len([p for p in env.parent.iterdir() if p.name.startswith(".env.bak-")]) == 2   # a new backup each time


def test_the_inbox_folder_comes_from_env_or_defaults_to_runtime_inbox(cfg, tmp_path, monkeypatch):
    assert cfg.inbox_dir_from("STORYFLOW_INBOX_DIR=D:/subs\n") == Path("D:/subs")
    assert cfg.inbox_dir_from('STORYFLOW_INBOX_DIR="D:/my subs"\n') == Path("D:/my subs")
    assert cfg.inbox_dir_from("STORYFLOW_INBOX_DIR=\nOTHER=1\n") == cfg.ROOT / "runtime" / "inbox"
    assert cfg.inbox_dir_from("") == cfg.ROOT / "runtime" / "inbox"
    env = tmp_path / ".env"
    target = tmp_path / "custom" / "inbox"
    env.write_text(f"STORYFLOW_INBOX_DIR={target}\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "ENV_FILE", env)
    assert cfg.ensure_inbox_dir() == target and target.is_dir()


def test_keeping_the_existing_env_still_offers_channel_links(cfg, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(OLD_ENV, encoding="utf-8", newline="")
    monkeypatch.setattr(cfg, "ENV_FILE", env)
    monkeypatch.setattr(cfg, "YES", False)
    monkeypatch.setattr(cfg, "VENV_PY", tmp_path / "python.exe")
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cfg, "ask_yes", lambda question, default=True: False)      # "no" to reconfigure
    monkeypatch.setattr(cfg, "configure_channels", lambda env_: calls.append("channels"))
    monkeypatch.setattr(cfg, "ensure_inbox_dir", lambda: calls.append("inbox"))
    monkeypatch.setattr(cfg, "configure_subtitles", lambda e: calls.append("SUBTITLES-MUST-NOT-RUN"))
    assert cfg.main() == 0
    assert calls == ["channels", "inbox"]
    assert env.read_bytes().decode("utf-8") == OLD_ENV                           # untouched
