"""HTTP API for multi-source ingestion: POST /workflows/{id}/sources, POST /workflows/{id}/sync,
GET /workflows/{id}/feeds. Real alembic-migrated SQLite, FakeVideoLister (no network), no sleeps."""

import re

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from storyflow.api.app import create_app
from storyflow.models import StoryProject
from storyflow.sources import FakeVideoLister, SourceError, VideoRef
from test_api import CONFIG, FORBIDDEN_KEYS, ROOTS, assert_error, make_runtime, new_workflow

CHANNEL = "https://www.youtube.com/@demo"
# a Windows drive path ("C:\..."), but NOT the "s:/" inside "https://"
ABS_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|(?<![\w.:])/(?:home|Users|tmp|var|etc|usr|mnt|root|opt|private)/")


def vid(n: int) -> str:
    return f"{n:011d}"        # 11 valid characters


def refs(*numbers, duration=600):
    return [VideoRef(vid(n), f"Video {n}", duration) for n in numbers]


def scan(node, where="body"):
    if isinstance(node, dict):
        for k, v in node.items():
            assert k not in FORBIDDEN_KEYS, f"{where}.{k}"
            scan(v, f"{where}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            scan(v, f"{where}[{i}]")
    elif isinstance(node, str):
        assert not ABS_PATH.search(node), f"absolute path in {where}: {node[:80]}"
        assert "sqlite:" not in node, f"db url in {where}"
        for root in ROOTS:
            assert root not in node, f"tmp root leaked in {where}"


class ScanClient(TestClient):
    def request(self, *args, **kwargs):
        resp = super().request(*args, **kwargs)
        if "json" in resp.headers.get("content-type", ""):
            scan(resp.json(), f"{args[0]} {args[1]}")
        return resp


@pytest.fixture
def rt(tmp_path):
    app = make_runtime(tmp_path, sleep=lambda s: None)
    app.ctx.video_lister = FakeVideoLister({CHANNEL: ("Demo", refs(1, 2, 3, 4, 5))})
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "my story.txt").write_text("Một dòng lời thoại.", encoding="utf-8")
    app.ctx.inbox_dir = inbox
    yield app
    app.close()


@pytest.fixture
def client(rt):
    return ScanClient(create_app(rt))


@pytest.fixture
def wf(client):
    return new_workflow(client, project=False)


def post_sources(client, wf, **body):
    return client.post(f"/api/workflows/{wf}/sources", json=body)


def projects_of(client, resp):
    """The workflow's projects: the sources / sync responses carry only a slim workflow view (a big batch must not
    come back as a megabyte of snapshot), so read the full snapshot separately."""
    return client.get(f"/api/workflows/{resp.json()['workflow']['id']}").json()["projects"]


def titles(client, resp):
    return [p["title"] for p in projects_of(client, resp)]


# ---------------------------------------------------------------- videos


def test_add_a_video_link_creates_one_project(client, wf):
    r = post_sources(client, wf, sources=["https://youtu.be/AAAAAAAAAAA"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["result"]["changed"] is True and body["result"]["workflow_id"] == wf
    assert [a["video_id"] for a in body["result"]["added"]] == ["AAAAAAAAAAA"]
    (project,) = projects_of(client, r)
    assert project["video_id"] == "AAAAAAAAAAA" and project["state"] == "not_started"
    assert project["title"] == "YouTube AAAAAAAAAAA" and project["feed_id"] is None
    assert client.get(f"/api/workflows/{wf}").json()["counts"]["not_started"] == 1


def test_repeating_the_same_video_is_a_safe_noop(client, wf):
    assert post_sources(client, wf, sources=["AAAAAAAAAAA"]).status_code == 201
    again = post_sources(client, wf, sources=["https://www.youtube.com/watch?v=AAAAAAAAAAA&list=PLxxxxxxxxxxxx"])
    assert again.status_code == 200
    result = again.json()["result"]
    assert result["changed"] is False and result["added"] == []
    assert [(d["video_id"], d["reason"]) for d in result["duplicates"]] == [("AAAAAAAAAAA", "in_workflow")]
    assert len(projects_of(client, again)) == 1


def test_same_video_twice_in_one_request_is_added_once(client, wf):
    r = post_sources(client, wf, sources=["AAAAAAAAAAA", "https://youtu.be/AAAAAAAAAAA"])
    assert r.status_code == 201
    assert len(r.json()["result"]["added"]) == 1
    assert [d["reason"] for d in r.json()["result"]["duplicates"]] == ["repeated"]


def test_reprocess_adds_the_video_again(client, wf):
    post_sources(client, wf, sources=["AAAAAAAAAAA"])
    r = post_sources(client, wf, sources=["AAAAAAAAAAA"], reprocess=True)
    assert r.status_code == 201 and len(projects_of(client, r)) == 2
    slugs = {p["slug"] for p in projects_of(client, r)}
    assert len(slugs) == 2


def test_video_already_processed_in_another_workflow_is_reported(client, wf):
    post_sources(client, wf, sources=["AAAAAAAAAAA"])
    other = new_workflow(client, name="other", project=False)
    r = post_sources(client, other, sources=["AAAAAAAAAAA"])
    assert r.status_code == 200
    (dup,) = r.json()["result"]["duplicates"]
    assert dup["reason"] == "already_processed" and dup["project_id"]
    assert projects_of(client, r) == []


def test_languages_are_stored_on_the_new_projects(client, rt, wf):
    post_sources(client, wf, sources=["AAAAAAAAAAA"], languages=["vi", "en"])
    with rt.session_factory() as db:
        (config,) = db.scalars(select(StoryProject.source_config)).all()
    assert config == {"kind": "video", "video_id": "AAAAAAAAAAA", "languages": ["vi", "en"]}


def test_inbox_source_creates_a_local_project_once(client, wf):
    r = post_sources(client, wf, sources=["inbox:my story.txt"])
    assert r.status_code == 201
    (project,) = projects_of(client, r)
    assert project["video_id"] is None and project["title"] == "my story"
    again = post_sources(client, wf, sources=["inbox:my story.txt"])
    assert again.status_code == 200 and again.json()["result"]["duplicates"][0]["video_id"] is None


# ---------------------------------------------------------------- channels and feeds


def test_channel_honours_limit_and_keeps_listing_order(client, rt, wf):
    r = post_sources(client, wf, sources=[CHANNEL], limit=3)
    assert r.status_code == 201, r.text
    assert titles(client, r) == ["Video 1", "Video 2", "Video 3"]                  # newest first, as listed
    (feed,) = r.json()["result"]["feeds"]
    assert (feed["kind"], feed["ref"], feed["title"], feed["listed"], feed["added"], feed["known"]) == (
        "channel", CHANNEL, "Demo", 3, 3, 3)
    assert rt.ctx.video_lister.calls == [("channel", CHANNEL, 3)]
    assert {p["feed_id"] for p in projects_of(client, r)} == {feed["id"]}


def test_default_limit_is_ten(client, rt, wf):
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", refs(*range(1, 13)))
    r = post_sources(client, wf, sources=[CHANNEL])
    assert r.status_code == 201 and len(r.json()["result"]["added"]) == 10
    assert rt.ctx.video_lister.calls[-1][2] == 10


def test_channel_repeat_reports_duplicates_and_adds_only_new(client, wf):
    assert post_sources(client, wf, sources=[CHANNEL], limit=3).status_code == 201
    r = post_sources(client, wf, sources=[CHANNEL], limit=5)
    assert r.status_code == 201
    result = r.json()["result"]
    assert [a["title"] for a in result["added"]] == ["Video 4", "Video 5"]
    assert [d["reason"] for d in result["duplicates"]] == ["in_workflow"] * 3
    (feed,) = result["feeds"]
    assert (feed["listed"], feed["added"], feed["known"]) == (5, 2, 5)
    third = post_sources(client, wf, sources=[CHANNEL], limit=5)
    assert third.status_code == 200 and third.json()["result"]["changed"] is False
    assert len(client.get(f"/api/workflows/{wf}/feeds").json()["feeds"]) == 1    # one feed row, updated in place


def test_min_duration_filters_out_short_videos(client, rt, wf):
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", [VideoRef(vid(1), "long", 900), VideoRef(vid(2), "short", 30),
                                                  VideoRef(vid(3), "unknown", None)])
    r = post_sources(client, wf, sources=[CHANNEL], min_duration_seconds=60)
    assert titles(client, r) == ["long", "unknown"]


def test_playlist_source_is_listed_too(client, rt, wf):
    rt.ctx.video_lister.store["PLabcdefghijk"] = ("My list", refs(7, 8))
    r = post_sources(client, wf, sources=["https://www.youtube.com/playlist?list=PLabcdefghijk"])
    assert r.status_code == 201 and titles(client, r) == ["Video 7", "Video 8"]
    (feed,) = client.get(f"/api/workflows/{wf}/feeds").json()["feeds"]
    assert (feed["kind"], feed["ref"], feed["title"]) == ("playlist", "PLabcdefghijk", "My list")


def test_feeds_endpoint_reports_cursor_and_counts(client, wf):
    assert client.get(f"/api/workflows/{wf}/feeds").json() == {"feeds": []}
    post_sources(client, wf, sources=[CHANNEL], limit=3, languages=["vi"])
    (feed,) = client.get(f"/api/workflows/{wf}/feeds").json()["feeds"]
    assert feed["title"] == "Demo" and feed["limit_count"] == 3 and feed["languages"] == ["vi"]
    assert feed["status"] == "active" and feed["known_count"] == 3 and feed["project_count"] == 3
    assert feed["skipped"] == 0 and feed["needs_attention"] == 0 and feed["last_error"] is None
    assert feed["last_scanned_at"]


def test_sync_adds_only_videos_the_ledger_has_not_seen(client, rt, wf):
    post_sources(client, wf, sources=[CHANNEL], limit=3)
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", refs(9, 1, 2, 3, 4, 5))   # a newer upload appeared
    r = client.post(f"/api/workflows/{wf}/sync")
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    # a re-scan looks at a wider window than the first add (limit x 3, at least 50): everything unseen is picked up
    assert result["changed"] is True and [a["title"] for a in result["added"]] == ["Video 9", "Video 4", "Video 5"]
    assert sorted(d["reason"] for d in result["duplicates"]) == ["in_workflow"] * 3
    assert rt.ctx.video_lister.calls[-1] == ("channel", CHANNEL, 50)
    assert len(projects_of(client, r)) == 6
    (feed,) = client.get(f"/api/workflows/{wf}/feeds").json()["feeds"]
    assert feed["known_count"] == 6
    quiet = client.post(f"/api/workflows/{wf}/sync")                             # nothing new: harmless
    assert quiet.status_code == 200 and quiet.json()["result"]["changed"] is False


def test_sync_without_feeds_is_a_noop(client, wf):
    post_sources(client, wf, sources=["AAAAAAAAAAA"])
    r = client.post(f"/api/workflows/{wf}/sync")
    assert r.status_code == 200
    assert r.json()["result"]["changed"] is False and r.json()["result"]["feeds"] == []
    assert len(projects_of(client, r)) == 1


def test_sync_reports_a_feed_that_cannot_be_listed_and_marks_it(client, rt, wf):
    post_sources(client, wf, sources=[CHANNEL], limit=3)
    rt.ctx.video_lister.store[CHANNEL] = SourceError("lister_failed", "could not list videos")
    r = client.post(f"/api/workflows/{wf}/sync")
    assert r.status_code == 200
    (err,) = r.json()["result"]["errors"]
    assert err["source"] == CHANNEL and err["code"] == "source_unreadable"
    (feed,) = client.get(f"/api/workflows/{wf}/feeds").json()["feeds"]
    assert feed["status"] == "error" and "could not list" in feed["last_error"]
    assert len(projects_of(client, r)) == 3                             # nothing was lost


# ---------------------------------------------------------------- validation (422)


@pytest.mark.parametrize("body", [
    {},
    {"sources": []},
    {"sources": ["AAAAAAAAAAA"] * 51},
    {"sources": [""]},
    {"sources": ["a" * 301]},
    {"sources": [5]},
    {"sources": "AAAAAAAAAAA"},
    {"sources": ["AAAAAAAAAAA"], "limit": 0},
    {"sources": ["AAAAAAAAAAA"], "limit": None},                # "everything" is never a legal request
    {"sources": ["AAAAAAAAAAA"], "limit": 1001},
    {"sources": ["AAAAAAAAAAA"], "limit": "ten"},
    {"sources": ["AAAAAAAAAAA"], "languages": []},
    {"sources": ["AAAAAAAAAAA"], "languages": ["a"] * 9},
    {"sources": ["AAAAAAAAAAA"], "languages": ["x" * 17]},
    {"sources": ["AAAAAAAAAAA"], "languages": [""]},
    {"sources": ["AAAAAAAAAAA"], "reprocess": "maybe"},
    {"sources": ["AAAAAAAAAAA"], "min_duration_seconds": -1},
    {"sources": ["AAAAAAAAAAA"], "min_duration_seconds": 86401},
    {"sources": ["AAAAAAAAAAA"], "surprise": True},
])
def test_invalid_bodies_are_rejected_and_create_nothing(client, wf, body):
    assert_error(client.post(f"/api/workflows/{wf}/sources", json=body), 422, "validation")
    assert client.get(f"/api/workflows/{wf}").json()["projects"] == []


def test_a_bad_source_string_names_its_index_and_adds_nothing(client, wf):
    err = assert_error(post_sources(client, wf, sources=["AAAAAAAAAAA", "not a link at all"]), 422, "validation")
    assert err["details"]["reason"] == "invalid_source" and err["details"]["index"] == 1
    assert client.get(f"/api/workflows/{wf}").json()["projects"] == []


@pytest.mark.parametrize("bad", ["https://example.com/watch?v=AAAAAAAAAAA", "inbox:../secret.txt",
                                 "inbox:C:/x.txt", "https://www.youtube.com/watch?v=short"])
def test_unsupported_sources_are_invalid_source(client, wf, bad):
    err = assert_error(post_sources(client, wf, sources=[bad]), 422, "validation")
    assert err["details"]["reason"] == "invalid_source"


def test_invalid_path_id_is_rejected(client):
    assert client.post("/api/workflows/bad id!/sources", json={"sources": ["AAAAAAAAAAA"]}).status_code == 422
    assert client.post("/api/workflows/bad id!/sync").status_code == 422
    assert client.get("/api/workflows/bad id!/feeds").status_code == 422


def test_oversized_body_is_refused_with_413(client, wf):
    big = b'{"sources": ["' + b"a" * (1024 * 1024 + 16) + b'"]}'
    r = client.post(f"/api/workflows/{wf}/sources", content=big, headers={"Content-Type": "application/json"})
    assert_error(r, 413, "validation")


# ---------------------------------------------------------------- not found / closed / unavailable


def test_unknown_workflow_is_404_everywhere(client):
    assert_error(client.post("/api/workflows/nope/sources", json={"sources": ["AAAAAAAAAAA"]}), 404, "not_found")
    assert_error(client.post("/api/workflows/nope/sync"), 404, "not_found")
    assert_error(client.get("/api/workflows/nope/feeds"), 404, "not_found")


def test_cancelled_workflow_is_closed_for_new_sources(client, wf):
    assert client.post(f"/api/workflows/{wf}/cancel").status_code == 200
    err = assert_error(post_sources(client, wf, sources=["AAAAAAAAAAA"]), 409, "invalid_state")
    assert err["details"]["reason"] == "workflow_closed"
    assert_error(client.post(f"/api/workflows/{wf}/sync"), 409, "invalid_state")


def test_channel_without_a_lister_is_503_but_videos_still_work(client, rt, wf):
    rt.ctx.video_lister = None
    err = assert_error(post_sources(client, wf, sources=[CHANNEL]), 503, "capacity_unavailable")
    assert err["details"]["reason"] == "lister_unavailable"
    assert client.get(f"/api/workflows/{wf}").json()["projects"] == []
    assert post_sources(client, wf, sources=["AAAAAAAAAAA"]).status_code == 201


def test_unreadable_channel_is_422_and_timeout_is_503(client, rt, wf):
    err = assert_error(post_sources(client, wf, sources=["https://www.youtube.com/@missing"]), 422, "validation")
    assert err["details"]["reason"] == "source_unreadable"
    rt.ctx.video_lister.store[CHANNEL] = SourceError("lister_timeout", "listing the channel timed out; try again later")
    err = assert_error(post_sources(client, wf, sources=[CHANNEL]), 503, "capacity_unavailable")
    assert err["details"]["reason"] == "lister_timeout"
    assert client.get(f"/api/workflows/{wf}").json()["projects"] == []


def test_new_source_projects_run_like_any_project_after_start(client, wf):
    post_sources(client, wf, sources=["AAAAAAAAAAA"])
    snap = client.post(f"/api/workflows/{wf}/start").json()["workflow"]
    assert snap["status"] == "active" and snap["projects"][0]["video_id"] == "AAAAAAAAAAA"


# ---------------------------------------------------------------- the slim workflow view


BRIEF_KEYS = {"id", "name", "status", "status_reason", "project_count", "counts"}


def test_sources_and_sync_responses_carry_a_slim_workflow_view(client, rt, wf):
    r = post_sources(client, wf, sources=[CHANNEL], limit=3)
    brief = r.json()["workflow"]
    assert set(brief) == BRIEF_KEYS and "projects" not in brief and "runners" not in brief
    assert brief["id"] == wf and brief["project_count"] == 3 and brief["counts"] == {"active": 3}
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", refs(9, 1, 2, 3, 4, 5))
    synced = client.post(f"/api/workflows/{wf}/sync").json()["workflow"]
    assert set(synced) == BRIEF_KEYS and synced["project_count"] == 6


def test_a_big_batch_response_stays_small(client, rt, wf):
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", refs(*range(1, 201)))
    r = post_sources(client, wf, sources=[CHANNEL], limit=200)
    assert r.status_code == 201 and len(r.json()["result"]["added"]) == 200
    assert len(r.json()["workflow"]) == len(BRIEF_KEYS)
    assert r.json()["workflow"]["project_count"] == 200
    full = client.get(f"/api/workflows/{wf}")
    assert len(r.content) < len(full.content) / 2                    # the snapshot is what made responses huge


def test_a_truncated_call_says_so_in_the_result(client, rt, wf):
    rt.ctx.video_lister.store[CHANNEL] = ("Demo", refs(*range(1, 6)))
    r = post_sources(client, wf, sources=[CHANNEL], limit=5)
    result = r.json()["result"]
    assert result["truncated"] is False and result["not_added"] == 0 and result["warnings"] == []
    assert result["duplicates_count"] == 0 and result["feeds"][0]["window_full"] is True
