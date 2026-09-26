"""storyflow.sources: source parsing, the yt-dlp lister (fake runner, no network, no subprocess), the fake lister,
and the inbox (operator-provided subtitle files). Deterministic: tmp_path only, an injected runner, no sleeps.

Regression tests are marked ``# regression`` (bugs found while writing this file):
  * ``v=ID%0A`` / ``list=ID%0A`` was accepted as an id (``$`` matches before a trailing newline);
  * a ``duration`` of ``inf`` crashed ``parse_lister_output`` with OverflowError;
  * a video title containing U+2028 was cut (``str.splitlines`` splits on it);
  * ``subtitle_text_to_plain`` dropped real dialogue ("Note that ...", "Style is ...", a bare "2024") and
    everything between a spoken ``<`` and ``>``, and leaked the WEBVTT header lines into the transcript.
"""

import os
import subprocess

import pytest

from storyflow.sources import (
    CHANNEL, FEED_KINDS, INBOX_NAME_RE, LOCAL, MAX_INBOX_BYTES, MAX_SOURCE_LENGTH, PLAYLIST, VIDEO, VIDEO_ID_RE,
    FakeVideoLister, ListedVideos, ParsedSource, SourceError, VideoLister, VideoRef, YtDlpLister, find_inbox_file,
    parse_lister_output, parse_source, read_inbox_text, subtitle_text_to_plain,
)

VID = "nap7Usq0lWE"
PL = "PLabcdefghijk"
UC = "UCSPIBINqRKAe_Ds-0-ZmyjQ"
CHANNEL_URL = "https://www.youtube.com/@GiaoGiaoAudio"


# ---------------------------------------------------------------------------- parse_source: accepted


@pytest.mark.parametrize("text,kind,ref", [
    # single videos
    (f"https://www.youtube.com/watch?v={VID}", VIDEO, VID),
    (f"http://www.youtube.com/watch?v={VID}", VIDEO, VID),
    (f"https://youtube.com/watch?v={VID}", VIDEO, VID),
    (f"https://m.youtube.com/watch?v={VID}", VIDEO, VID),
    (f"https://music.youtube.com/watch?v={VID}", VIDEO, VID),
    (f"www.youtube.com/watch?v={VID}", VIDEO, VID),
    (f"youtube.com/watch?v={VID}", VIDEO, VID),
    (f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}", VIDEO, VID),
    (f"https://www.youtube.com/watch?v={VID}&list={PL}&index=3", VIDEO, VID),        # a watch link is that video
    (f"https://www.youtube.com/watch?feature=share&v={VID}", VIDEO, VID),
    (f"https://youtu.be/{VID}", VIDEO, VID),
    (f"youtu.be/{VID}", VIDEO, VID),
    (f"https://youtu.be/{VID}?t=42", VIDEO, VID),
    (f"https://www.youtube.com/shorts/{VID}", VIDEO, VID),
    (f"https://www.youtube.com/live/{VID}?feature=share", VIDEO, VID),
    (f"https://www.youtube.com/embed/{VID}", VIDEO, VID),
    (VID, VIDEO, VID),
    (f"  {VID}  ", VIDEO, VID),
    ("a-b_c-d_e-f", VIDEO, "a-b_c-d_e-f"),
    # playlists
    (f"https://www.youtube.com/playlist?list={PL}", PLAYLIST, PL),
    (f"https://www.youtube.com/playlist?list={PL}&si=xyz", PLAYLIST, PL),
    (f"https://m.youtube.com/playlist?si=xyz&list={PL}", PLAYLIST, PL),
    # channels
    ("@GiaoGiaoAudio", CHANNEL, CHANNEL_URL),
    (CHANNEL_URL, CHANNEL, CHANNEL_URL),
    (f"{CHANNEL_URL}/videos", CHANNEL, CHANNEL_URL),
    (f"{CHANNEL_URL}/featured?view=0", CHANNEL, CHANNEL_URL),
    ("youtube.com/@GiaoGiaoAudio", CHANNEL, CHANNEL_URL),
    ("@Phù-Thuỷ.Audio_2", CHANNEL, "https://www.youtube.com/@Phù-Thuỷ.Audio_2"),
    (f"https://www.youtube.com/channel/{UC}", CHANNEL, f"https://www.youtube.com/channel/{UC}"),
    (f"https://www.youtube.com/channel/{UC}/videos", CHANNEL, f"https://www.youtube.com/channel/{UC}"),
    (f"https://www.youtube.com/channel/{UC}/", CHANNEL, f"https://www.youtube.com/channel/{UC}"),
    ("https://www.youtube.com/c/SomeName", CHANNEL, "https://www.youtube.com/c/SomeName"),
    ("https://www.youtube.com/c/SomeName/videos", CHANNEL, "https://www.youtube.com/c/SomeName"),
    ("https://www.youtube.com/user/Some.User-1", CHANNEL, "https://www.youtube.com/user/Some.User-1"),
    # inbox files
    ("inbox:abc.txt", LOCAL, "abc.txt"),
    ("INBOX:abc.srt", LOCAL, "abc.srt"),
    ("Inbox:abc.vtt", LOCAL, "abc.vtt"),
    ("inbox:  my story.txt  ", LOCAL, "my story.txt"),
    ("inbox:truyện hay - tập 1.txt", LOCAL, "truyện hay - tập 1.txt"),
    ("inbox:a-b_c.d.txt", LOCAL, "a-b_c.d.txt"),
    ("inbox:" + "a" * 119 + ".txt", LOCAL, "a" * 119 + ".txt"),                       # longest allowed stem
])
def test_parse_source_accepts(text, kind, ref):
    parsed = parse_source(text)
    assert (parsed.kind, parsed.ref) == (kind, ref)
    assert parsed.original == text.strip()


def test_parsed_source_is_immutable_and_feed_kinds_are_the_listable_ones():
    parsed = parse_source(VID)
    with pytest.raises(AttributeError):
        parsed.kind = "channel"
    assert FEED_KINDS == (PLAYLIST, CHANNEL)
    assert isinstance(parsed, ParsedSource)


# ---------------------------------------------------------------------------- parse_source: rejected


@pytest.mark.parametrize("text", [
    "", "   ", "\n\t ", None, 123, 12.5, b"abcdefghijk", ["abcdefghijk"], {"v": VID},
    "x" * (MAX_SOURCE_LENGTH + 1),
    f"https://www.youtube.com/watch?v={VID}&x=" + "y" * MAX_SOURCE_LENGTH,
    # not YouTube
    "https://vimeo.com/123456", "https://example.com/watch?v=" + VID, f"https://youtube.com.evil.com/watch?v={VID}",
    f"https://notyoutube.com/watch?v={VID}", f"https://evil.com/?u=youtube.com/watch?v={VID}", "hello world",
    "https://[",
    # watch / youtu.be
    "https://www.youtube.com/watch", "https://www.youtube.com/watch?v=", "https://www.youtube.com/watch?v=short",
    f"https://www.youtube.com/watch?v={VID}x", f"https://www.youtube.com/watch?list={PL}", "https://youtu.be/",
    "https://youtu.be/short", f"https://youtu.be/{VID}extra",
    # shorts / live / embed without a usable id
    "https://www.youtube.com/shorts/", "https://www.youtube.com/shorts/short", "https://www.youtube.com/live/",
    "https://www.youtube.com/embed/x",
    # playlist
    "https://www.youtube.com/playlist", "https://www.youtube.com/playlist?list=", "https://www.youtube.com/playlist?list=short",
    f"https://www.youtube.com/playlist?v={VID}",
    # channel-ish but unusable
    "https://www.youtube.com/", "https://www.youtube.com/feed/subscriptions", "https://www.youtube.com/results?search_query=x",
    "https://www.youtube.com/channel/notauid", "https://www.youtube.com/channel/UCshort", "https://www.youtube.com/channel/",
    "https://www.youtube.com/c/", "https://www.youtube.com/user/", "https://www.youtube.com/@", "@",
    # inbox: must be a bare .txt/.srt/.vtt name
    "inbox:", "inbox:   ", "inbox:notes", "inbox:notes.pdf", "inbox:notes.txt.exe", "inbox:.txt", "inbox:sub/a.txt",
    "inbox:sub\\a.txt", "inbox:/etc/passwd.txt", "inbox:..\\a.txt", "inbox:../a.txt", "inbox:a..txt",
    "inbox:C:\\a.txt", "inbox:C:a.txt", "inbox:" + "a" * 120 + ".txt",
])
def test_parse_source_rejects(text):
    with pytest.raises(SourceError) as exc:
        parse_source(text)
    assert exc.value.code == "invalid_source"
    assert isinstance(exc.value, ValueError)
    assert str(exc.value)                          # a short, human readable message
    assert "\\" not in str(exc.value) and "/" not in str(exc.value).replace("/.srt/.vtt", "")  # never echoes input paths


def test_id_with_a_trailing_newline_is_not_an_id():   # regression
    assert not VIDEO_ID_RE.match(VID + "\n")
    assert not INBOX_NAME_RE.match("a.txt\n")
    for encoded in (f"https://www.youtube.com/watch?v={VID}%0A", f"https://www.youtube.com/watch?v={VID}%0a",
                    f"https://www.youtube.com/playlist?list={PL}%0A"):
        with pytest.raises(SourceError):
            parse_source(encoded)


def test_source_error_carries_a_stable_code():
    exc = SourceError("lister_failed", "nope")
    assert (exc.code, str(exc)) == ("lister_failed", "nope")


@pytest.mark.parametrize("name", ["a.txt", "1.srt", "my story.vtt", "truyện.txt", "a-b_c.d.txt", "a b.c.txt"])
def test_inbox_name_pattern_accepts(name):
    assert INBOX_NAME_RE.match(name)


@pytest.mark.parametrize("name", ["", ".txt", " a.txt", "a", "a.md", "a.txt.md", "a/b.txt", "a\\b.txt", "a:b.txt",
                                  "a\n.txt", "a\t.txt", "a*.txt", "a?.txt", "a|b.txt", "a" * 120 + ".txt"])
def test_inbox_name_pattern_rejects(name):
    assert not INBOX_NAME_RE.match(name)


# ---------------------------------------------------------------------------- YtDlpLister: url_for / argv


def test_url_for_channel_targets_the_uploads_tab():
    assert YtDlpLister.url_for(ParsedSource(CHANNEL, CHANNEL_URL)) == CHANNEL_URL + "/videos"
    assert YtDlpLister.url_for(ParsedSource(CHANNEL, CHANNEL_URL + "/")) == CHANNEL_URL + "/videos"   # no "//videos"
    assert YtDlpLister.url_for(ParsedSource(CHANNEL, f"https://www.youtube.com/channel/{UC}")) == \
        f"https://www.youtube.com/channel/{UC}/videos"


def test_url_for_playlist():
    assert YtDlpLister.url_for(ParsedSource(PLAYLIST, PL)) == f"https://www.youtube.com/playlist?list={PL}"


@pytest.mark.parametrize("kind", [VIDEO, LOCAL, "bogus"])
def test_url_for_rejects_kinds_that_cannot_be_listed(kind):
    with pytest.raises(SourceError) as exc:
        YtDlpLister.url_for(ParsedSource(kind, "x"))
    assert exc.value.code == "invalid_source"


def test_argv_shape():
    lister = YtDlpLister(python="C:\\py\\python.exe")
    argv = lister.argv(ParsedSource(CHANNEL, CHANNEL_URL), 5)
    assert argv[:2] == ["C:\\py\\python.exe", "-m"] and argv[2] == "yt_dlp"
    for flag in ("--flat-playlist", "--no-warnings", "--ignore-errors", "--ignore-config", "--no-cache-dir"):
        assert flag in argv                               # a user's yt-dlp.conf must never change the listing
    assert argv[argv.index("--socket-timeout") + 1] == "20" and argv[argv.index("--extractor-retries") + 1] == "2"
    assert argv[argv.index("--playlist-end") + 1] == "5"
    assert argv[argv.index("--print") + 1] == "%(.{id,duration,playlist_title,title})j"   # one JSON object per entry
    assert argv[-1] == CHANNEL_URL + "/videos"                                   # the URL is the last argument


@pytest.mark.parametrize("limit", [None, 0])
def test_argv_has_no_playlist_end_without_a_limit(limit):
    assert "--playlist-end" not in YtDlpLister().argv(ParsedSource(PLAYLIST, PL), limit)


def test_argv_limit_is_a_plain_integer():
    argv = YtDlpLister().argv(ParsedSource(PLAYLIST, PL), 10.0)
    assert argv[argv.index("--playlist-end") + 1] == "10"


def test_default_python_is_the_current_interpreter():
    import sys
    assert YtDlpLister().python == sys.executable
    assert YtDlpLister().timeout == 120.0


# ---------------------------------------------------------------------------- YtDlpLister.list_videos (fake runner)


def row(video_id, duration="100", playlist="Giao Giao Audio - Videos", title="A title"):
    return "\t".join([video_id, duration, playlist, title])


class FakeRun:
    """Stands in for subprocess.run: records every call, returns a canned CompletedProcess or raises."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, raises=None):
        self.stdout, self.stderr, self.returncode, self.raises = stdout, stderr, returncode, raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def list_with(run, source=None, limit=None, **lister_kwargs):
    lister = YtDlpLister(python="py", runner=run, **lister_kwargs)
    return lister.list_videos(source or ParsedSource(CHANNEL, CHANNEL_URL), limit)


def test_list_videos_parses_rows_newest_first():
    out = "\n".join([
        row("NQyV_6XXyMg", "1464", title="Thiên kim giả rất thích bắt chước tôi"),
        row("wKTTzhA648g", "1673", title="Mẹ mua vé tàu hỏa ghế cứng cho cả nhà"),
        row(VID, "1988", title="Nghe thấy tiếng lòng bạn thân vào ngày cưới"),
    ]) + "\n"
    listed = list_with(FakeRun(stdout=out.encode("utf-8")))
    assert isinstance(listed, ListedVideos)
    assert listed.title == "Giao Giao Audio"                      # " - Videos" tab suffix stripped
    assert listed.videos == [
        VideoRef("NQyV_6XXyMg", "Thiên kim giả rất thích bắt chước tôi", 1464),
        VideoRef("wKTTzhA648g", "Mẹ mua vé tàu hỏa ghế cứng cho cả nhà", 1673),
        VideoRef(VID, "Nghe thấy tiếng lòng bạn thân vào ngày cưới", 1988),
    ]


def test_list_videos_accepts_str_stdout_too():
    listed = list_with(FakeRun(stdout=row(VID) + "\n"))
    assert [v.video_id for v in listed.videos] == [VID]


def test_list_videos_runs_the_subprocess_safely():
    run = FakeRun(stdout=row(VID))
    list_with(run, ParsedSource(PLAYLIST, PL), limit=3, timeout=45.0)
    (cmd, kwargs), = run.calls
    assert cmd == YtDlpLister(python="py").argv(ParsedSource(PLAYLIST, PL), 3)
    assert kwargs["capture_output"] is True and kwargs["timeout"] == pytest.approx(45.0, abs=1.0)
    assert kwargs["stdin"] == subprocess.DEVNULL                    # a listing must never wait for input
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8" and kwargs["env"]["PYTHONUTF8"] == "1"
    assert "shell" not in kwargs                                    # argv list, never a shell string


def test_list_videos_skips_private_deleted_and_unavailable_rows():
    out = "\n".join([row("aaaaaaaaaa1", title="[Private video]"), row("aaaaaaaaaa2", title="[deleted video]"),
                     row("aaaaaaaaaa3", title="[UNAVAILABLE VIDEO]"), row("aaaaaaaaaa4", title="Real one")])
    listed = list_with(FakeRun(stdout=out))
    assert [v.video_id for v in listed.videos] == ["aaaaaaaaaa4"]


def test_list_videos_ignores_malformed_and_duplicate_rows():
    out = "\n".join([
        "WARNING: something the tool printed",
        "",
        "only\tthree\tcolumns",
        row("short"),                         # not an 11-char id
        row("waytoolongid1"),                 # too long
        row("bad!idchars"),                   # illegal character
        row("aaaaaaaaaa1", title="first"),
        row("aaaaaaaaaa1", title="second"),   # duplicate id: the first row wins
        row("aaaaaaaaaa2", title="kept"),
    ])
    listed = list_with(FakeRun(stdout=out))
    assert [(v.video_id, v.title) for v in listed.videos] == [("aaaaaaaaaa1", "first"), ("aaaaaaaaaa2", "kept")]


def test_list_videos_handles_missing_fields_as_none():
    out = "\n".join([row("aaaaaaaaaa1", duration="NA", title="NA"), row("aaaaaaaaaa2", duration="", title=""),
                     row("aaaaaaaaaa3", duration="12.7", title="decimal"), row("aaaaaaaaaa4", duration="abc")])
    videos = {v.video_id: v for v in list_with(FakeRun(stdout=out)).videos}
    assert (videos["aaaaaaaaaa1"].duration_seconds, videos["aaaaaaaaaa1"].title) == (None, None)
    assert (videos["aaaaaaaaaa2"].duration_seconds, videos["aaaaaaaaaa2"].title) == (None, None)
    assert videos["aaaaaaaaaa3"].duration_seconds == 12                 # truncated, not rounded
    assert videos["aaaaaaaaaa4"].duration_seconds is None


def test_list_videos_never_raises_on_undecodable_output():
    listed = list_with(FakeRun(stdout=row("aaaaaaaaaa1", title="ok").encode() + b"\n\xff\xfe\xfa\n"))
    assert [v.video_id for v in listed.videos] == ["aaaaaaaaaa1"]


def test_empty_channel_is_an_empty_result_not_an_error():
    listed = list_with(FakeRun(stdout=b"", stderr=b"", returncode=0))
    assert listed.videos == [] and listed.title is None


@pytest.mark.parametrize("returncode,stderr", [(1, b""), (1, b"ERROR: nope"), (0, b"ERROR: [youtube] not found"),
                                               (2, b"WARNING: x")])
def test_no_videos_and_an_error_is_lister_failed(returncode, stderr):
    with pytest.raises(SourceError) as exc:
        list_with(FakeRun(stdout=b"", stderr=stderr, returncode=returncode))
    assert exc.value.code == "lister_failed"


def test_none_stdout_is_tolerated():
    run = FakeRun(stdout=None, stderr=None, returncode=1)
    with pytest.raises(SourceError) as exc:
        list_with(run)
    assert exc.value.code == "lister_failed"


@pytest.mark.parametrize("returncode", [0, 1])
def test_missing_module_is_lister_unavailable_with_an_install_hint(returncode):
    run = FakeRun(stderr=b"C:\\py\\python.exe: No module named yt_dlp", returncode=returncode)
    with pytest.raises(SourceError) as exc:
        list_with(run)
    assert exc.value.code == "lister_unavailable"
    assert "requirements-channel" in str(exc.value)
    assert "python.exe" not in str(exc.value)                          # stderr / paths are never echoed


def test_timeout_is_lister_timeout():
    with pytest.raises(SourceError) as exc:
        list_with(FakeRun(raises=subprocess.TimeoutExpired(cmd="py", timeout=1)))
    assert exc.value.code == "lister_timeout"


@pytest.mark.parametrize("error", [FileNotFoundError("C:\\secret\\python.exe"), PermissionError("denied"), OSError("boom")])
def test_cannot_start_is_lister_unavailable_and_hides_the_path(error):
    with pytest.raises(SourceError) as exc:
        list_with(FakeRun(raises=error))
    assert exc.value.code == "lister_unavailable"
    assert "secret" not in str(exc.value) and "python.exe" not in str(exc.value)


def test_partial_output_with_a_failing_exit_still_returns_the_videos():
    run = FakeRun(stdout=row("aaaaaaaaaa1") + "\n" + row("aaaaaaaaaa2"), stderr=b"ERROR: one entry failed", returncode=1)
    assert [v.video_id for v in list_with(run).videos] == ["aaaaaaaaaa1", "aaaaaaaaaa2"]


def test_list_videos_rejects_a_source_that_cannot_be_listed_before_running_anything():
    run = FakeRun()
    with pytest.raises(SourceError) as exc:
        list_with(run, ParsedSource(VIDEO, VID))
    assert exc.value.code == "invalid_source" and run.calls == []


# ---------------------------------------------------------------------------- parse_lister_output


def test_parse_lister_output_empty():
    assert parse_lister_output("") == ListedVideos(None, [])
    assert parse_lister_output("\n\n") == ListedVideos(None, [])


def test_parse_lister_output_handles_crlf_and_ids_with_padding():
    out = row(" aaaaaaaaaa1 ", title="  padded  ") + "\r\n" + row("aaaaaaaaaa2") + "\r\n"
    listed = parse_lister_output(out)
    assert [(v.video_id, v.title) for v in listed.videos] == [("aaaaaaaaaa1", "padded"), ("aaaaaaaaaa2", "A title")]


def test_parse_lister_output_keeps_tabs_inside_the_title():
    listed = parse_lister_output(row(VID, title="Part 1\tPart 2\tPart 3"))
    assert listed.videos[0].title == "Part 1\tPart 2\tPart 3"


def test_playlist_title_is_the_first_real_one():
    out = "\n".join([row("aaaaaaaaaa1", playlist="NA"), row("aaaaaaaaaa2", playlist=""),
                     row("aaaaaaaaaa3", playlist="Real Name - Videos"), row("aaaaaaaaaa4", playlist="Other")])
    assert parse_lister_output(out).title == "Real Name"


@pytest.mark.parametrize("raw,clean", [
    ("Chan - Videos", "Chan"), ("Chan - Video", "Chan"), ("My list - Playlist", "My list"),
    ("Videos", "Videos"), ("Chan - Shorts", "Chan - Shorts"), ("A - Videos - Videos", "A - Videos"),
    ("Chan Videos", "Chan Videos"),
])
def test_channel_title_suffix_cleanup(raw, clean):
    assert parse_lister_output(row(VID, playlist=raw)).title == clean


def test_playlist_title_is_capped_at_255_chars():
    assert len(parse_lister_output(row(VID, playlist="x" * 400)).title) == 255


@pytest.mark.parametrize("duration", ["inf", "-inf", "nan", "abc", "", "NA", "1e999", "-5"])
def test_odd_durations_become_none_and_never_raise(duration):   # regression (inf -> OverflowError)
    (video,) = parse_lister_output(row(VID, duration=duration)).videos
    assert video.duration_seconds is None


def test_durations_are_whole_seconds():
    assert parse_lister_output(row(VID, duration="0")).videos[0].duration_seconds == 0
    assert parse_lister_output(row(VID, duration="3600.9")).videos[0].duration_seconds == 3600


def test_title_with_unicode_line_separator_is_not_cut():   # regression (splitlines)
    for sep in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c"):
        title = f"Before{sep}After"
        (video,) = parse_lister_output(row(VID, title=title)).videos
        assert video.title == title                              # the row survived whole, incl. what follows the separator


# ---------------------------------------------------------------------------- FakeVideoLister


VIDEOS = [VideoRef("aaaaaaaaaa1", "one", 60), VideoRef("aaaaaaaaaa2", "two", 120), VideoRef("aaaaaaaaaa3", "three", 180)]


def test_fake_lister_returns_title_and_videos_and_records_calls():
    fake = FakeVideoLister({CHANNEL_URL: ("Giao Giao", VIDEOS)})
    listed = fake.list_videos(ParsedSource(CHANNEL, CHANNEL_URL), None)
    assert listed.title == "Giao Giao" and listed.videos == VIDEOS
    assert fake.calls == [(CHANNEL, CHANNEL_URL, None)]
    assert isinstance(fake, VideoLister)


@pytest.mark.parametrize("limit,count", [(None, 3), (0, 3), (1, 1), (2, 2), (3, 3), (99, 3)])
def test_fake_lister_limit(limit, count):
    fake = FakeVideoLister({PL: ("List", VIDEOS)})
    assert len(fake.list_videos(ParsedSource(PLAYLIST, PL), limit).videos) == count
    assert fake.calls == [(PLAYLIST, PL, limit)]


def test_fake_lister_returns_a_copy():
    fake = FakeVideoLister({PL: ("List", VIDEOS)})
    fake.list_videos(ParsedSource(PLAYLIST, PL), None).videos.clear()
    assert len(fake.list_videos(ParsedSource(PLAYLIST, PL), None).videos) == 3


def test_fake_lister_unknown_ref_is_lister_failed_and_still_recorded():
    fake = FakeVideoLister()
    with pytest.raises(SourceError) as exc:
        fake.list_videos(ParsedSource(CHANNEL, CHANNEL_URL), 5)
    assert exc.value.code == "lister_failed" and fake.calls == [(CHANNEL, CHANNEL_URL, 5)]


def test_fake_lister_can_be_scripted_to_fail():
    fake = FakeVideoLister({PL: SourceError("lister_timeout", "slow")})
    with pytest.raises(SourceError) as exc:
        fake.list_videos(ParsedSource(PLAYLIST, PL), None)
    assert exc.value.code == "lister_timeout"


def test_the_abstract_lister_cannot_be_instantiated():
    with pytest.raises(TypeError):
        VideoLister()


# ---------------------------------------------------------------------------- inbox


@pytest.fixture
def inbox(tmp_path):
    path = tmp_path / "inbox"
    path.mkdir()
    return path


def test_find_inbox_file_none_when_absent(inbox):
    assert find_inbox_file(inbox, VID) is None


@pytest.mark.parametrize("suffix", [".txt", ".srt", ".vtt"])
def test_find_inbox_file_returns_the_file_name(inbox, suffix):
    (inbox / (VID + suffix)).write_text("x", encoding="utf-8")
    assert find_inbox_file(inbox, VID) == VID + suffix
    assert find_inbox_file(str(inbox), VID) == VID + suffix          # a plain string works too


def test_find_inbox_file_precedence_is_txt_then_srt_then_vtt(inbox):
    (inbox / (VID + ".vtt")).write_text("v", encoding="utf-8")
    assert find_inbox_file(inbox, VID) == VID + ".vtt"
    (inbox / (VID + ".srt")).write_text("s", encoding="utf-8")
    assert find_inbox_file(inbox, VID) == VID + ".srt"
    (inbox / (VID + ".txt")).write_text("t", encoding="utf-8")
    assert find_inbox_file(inbox, VID) == VID + ".txt"


def test_find_inbox_file_ignores_directories_and_other_videos(inbox):
    (inbox / (VID + ".txt")).mkdir()                                  # a directory named like a transcript
    (inbox / "zzzzzzzzzzz.txt").write_text("other", encoding="utf-8")
    assert find_inbox_file(inbox, VID) is None
    (inbox / (VID + ".srt")).write_text("s", encoding="utf-8")
    assert find_inbox_file(inbox, VID) == VID + ".srt"


@pytest.mark.parametrize("video_id", [None, "", "short", VID + "x", "../" + VID[:8], VID[:10] + "/", VID + "\n", 5])
def test_find_inbox_file_rejects_anything_that_is_not_a_video_id(inbox, video_id):
    (inbox / "short.txt").write_text("x", encoding="utf-8")
    assert find_inbox_file(inbox, video_id) is None


def test_find_inbox_file_without_an_inbox_dir(tmp_path):
    assert find_inbox_file(None, VID) is None
    assert find_inbox_file(tmp_path / "does-not-exist", VID) is None


def test_read_inbox_text_plain_text_is_stripped(inbox):
    (inbox / "a.txt").write_text("  \n Hello world\nsecond line \n\n", encoding="utf-8")
    assert read_inbox_text(inbox, "a.txt") == "Hello world\nsecond line"


def test_read_inbox_text_keeps_vietnamese_and_drops_a_bom(inbox):
    text = "Tôi đã nghe thấy tiếng lòng của cô bạn thân.\nLâm Hạ đúng là đồ ngu ngốc."
    (inbox / "vi.txt").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    assert read_inbox_text(inbox, "vi.txt") == text
    (inbox / "truyện hay.txt").write_text(text, encoding="utf-8")
    assert read_inbox_text(inbox, "truyện hay.txt") == text


def test_read_inbox_text_converts_srt_and_vtt(inbox):
    srt = "1\n00:00:01,000 --> 00:00:02,000\nHello <i>there</i>\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n"
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello there\n\n00:00:03.000 --> 00:00:04.000\nGeneral Kenobi\n"
    (inbox / "a.srt").write_text(srt, encoding="utf-8-sig")
    (inbox / "a.vtt").write_text(vtt, encoding="utf-8")
    assert read_inbox_text(inbox, "a.srt") == "Hello there\nGeneral Kenobi"
    assert read_inbox_text(inbox, "a.vtt") == "Hello there\nGeneral Kenobi"


def test_read_inbox_text_txt_is_not_run_through_the_subtitle_converter(inbox):
    body = "1\n00:00:01,000 --> 00:00:02,000\nkept as typed"
    (inbox / "a.txt").write_text(body, encoding="utf-8")
    assert read_inbox_text(inbox, "a.txt") == body


@pytest.mark.parametrize("eol", ["\r\n", "\r", "\n"])
def test_read_inbox_text_normalises_line_endings(inbox, eol):   # Notepad saves CRLF
    (inbox / "a.txt").write_bytes(eol.join(["one", "two", "", "three"]).encode("utf-8"))
    assert read_inbox_text(inbox, "a.txt") == "one\ntwo\n\nthree"


@pytest.mark.parametrize("bad", [5, 12.5, b"a.txt", ["a.txt"], object()])
def test_inbox_helpers_never_raise_on_wrong_typed_input(inbox, bad):   # regression (TypeError from re.match)
    (inbox / "a.txt").write_text("x", encoding="utf-8")
    assert read_inbox_text(inbox, bad) is None
    assert find_inbox_file(inbox, bad) is None


def test_read_inbox_text_empty_file_is_an_empty_string_not_none(inbox):
    (inbox / "empty.txt").write_text("", encoding="utf-8")
    assert read_inbox_text(inbox, "empty.txt") == ""


def test_read_inbox_text_missing_or_no_inbox(tmp_path, inbox):
    assert read_inbox_text(inbox, "nope.txt") is None
    assert read_inbox_text(None, "a.txt") is None
    assert read_inbox_text(tmp_path / "does-not-exist", "a.txt") is None


def test_read_inbox_text_directory_named_like_a_file_is_none(inbox):
    (inbox / "dir.txt").mkdir()
    assert read_inbox_text(inbox, "dir.txt") is None


@pytest.mark.parametrize("name", ["", None, "a.pdf", "a", ".txt", "../secret.txt", "..\\secret.txt", "sub/a.txt",
                                  "sub\\a.txt", "/abs.txt", "C:\\secret.txt", "a..txt", "a.txt\n"])
def test_read_inbox_text_refuses_unsafe_names(tmp_path, inbox, name):
    (tmp_path / "secret.txt").write_text("outside the inbox", encoding="utf-8")
    (inbox / "sub").mkdir()
    (inbox / "sub" / "a.txt").write_text("nested", encoding="utf-8")
    (inbox / "a.pdf").write_text("pdf", encoding="utf-8")
    assert read_inbox_text(inbox, name) is None


def test_read_inbox_text_size_cap(inbox):
    (inbox / "big.txt").write_bytes(b"a" * (MAX_INBOX_BYTES + 1))
    (inbox / "max.txt").write_bytes(b"a" * MAX_INBOX_BYTES)
    assert read_inbox_text(inbox, "big.txt") is None
    assert read_inbox_text(inbox, "max.txt") == "a" * MAX_INBOX_BYTES


def test_read_inbox_text_binary_junk_is_none(inbox):
    (inbox / "bin.txt").write_bytes(b"\x00\x01\x02\x03 binary")
    (inbox / "undefined.txt").write_bytes(b"\x81\x8d\x8f\x90")      # undefined in cp1252, invalid UTF-8
    assert read_inbox_text(inbox, "bin.txt") is None
    assert read_inbox_text(inbox, "undefined.txt") is None


def test_read_inbox_text_falls_back_to_cp1252_for_old_ansi_files(inbox):
    (inbox / "cp1252.srt").write_bytes("caf\xe9".encode("cp1252"))
    assert read_inbox_text(inbox, "cp1252.srt") == "caf\xe9"


def test_read_inbox_text_refuses_a_symlink_that_escapes_the_inbox(tmp_path, inbox):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = inbox / "link.txt"
    id_link = inbox / (VID + ".txt")
    try:
        os.symlink(outside, link)
        os.symlink(outside, id_link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    assert read_inbox_text(inbox, "link.txt") is None
    assert read_inbox_text(inbox, VID + ".txt") is None
    assert find_inbox_file(inbox, VID) is None                # the escaping link is not offered as a source


# ---------------------------------------------------------------------------- subtitle_text_to_plain


def test_srt_numbers_and_timings_are_removed():
    srt = "\n".join([
        "1", "00:00:01,000 --> 00:00:02,500", "First line", "still the first cue", "",
        "2", "00:00:03,000 --> 00:00:04,000", "Second cue", "",
        "10", "01:02:03,456 --> 01:02:05,000", "Late cue", "",
    ])
    assert subtitle_text_to_plain(srt) == "First line\nstill the first cue\nSecond cue\nLate cue"


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_line_endings_do_not_matter(eol):
    srt = eol.join(["1", "00:00:01,000 --> 00:00:02,000", "One", "", "2", "00:00:03,000 --> 00:00:04,000", "Two", ""])
    assert subtitle_text_to_plain(srt) == "One\nTwo"


def test_sloppy_srt_without_blank_lines_between_cues():
    srt = "\n".join(["1", "00:00:01,000 --> 00:00:02,000", "Hello", "2", "00:00:03,000 --> 00:00:04,000", "World"])
    assert subtitle_text_to_plain(srt) == "Hello\nWorld"


def test_vtt_header_block_is_dropped_completely():   # regression (Kind:/Language: used to leak into the transcript)
    vtt = "\n".join(["WEBVTT", "Kind: captions", "Language: vi", "X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:00:00.000", "",
                     "00:00:01.000 --> 00:00:02.000", "Xin chào", ""])
    assert subtitle_text_to_plain(vtt) == "Xin chào"


def test_vtt_header_with_a_title_and_a_header_only_file():
    assert subtitle_text_to_plain("WEBVTT - some title\n\n00:00:01.000 --> 00:00:02.000\nHi\n") == "Hi"
    assert subtitle_text_to_plain("WEBVTT\n") == ""
    assert subtitle_text_to_plain("WEBVTT") == ""


def test_vtt_note_style_and_region_blocks_are_dropped():
    vtt = "\n".join([
        "WEBVTT", "",
        "NOTE", "This is a multi-line", "comment", "",
        "NOTE one-line comment", "",
        "STYLE", "::cue {", "  background-color: black;", "}", "",
        "REGION", "id:fred", "width:40%", "",
        "00:00:01.000 --> 00:00:02.000", "Kept", "",
    ])
    assert subtitle_text_to_plain(vtt) == "Kept"


def test_vtt_cue_identifiers_and_cue_settings_are_dropped():
    vtt = "\n".join([
        "WEBVTT", "",
        "intro", "00:00:01.000 --> 00:00:02.000 align:start position:0%", "Line one", "",
        "42", "00:02.000 --> 00:03.000", "Short timestamp form", "",
    ])
    assert subtitle_text_to_plain(vtt) == "Line one\nShort timestamp form"


@pytest.mark.parametrize("raw,clean", [
    ("<i>italic</i>", "italic"), ("<b>bold</b> text", "bold text"), ("<c.colorE5E5E5>colour</c>", "colour"),
    ("<v Nam>Xin chào</v>", "Xin chào"), ("Hello<00:00:01.500> there", "Hello there"),
    ("<font color=\"#ffff00\">yellow</font>", "yellow"), ("<u>under</u><i>x</i>", "underx"),
])
def test_markup_tags_are_stripped(raw, clean):
    assert subtitle_text_to_plain(f"00:00:01.000 --> 00:00:02.000\n{raw}\n") == clean


def test_consecutive_duplicate_lines_collapse_but_repeats_elsewhere_stay():
    text = "00:00:01.000 --> 00:00:02.000\nsame\nsame\n\n00:00:03.000 --> 00:00:04.000\nsame\nother\nsame\n"
    assert subtitle_text_to_plain(text) == "same\nother\nsame"


def test_blank_and_whitespace_only_input():
    assert subtitle_text_to_plain("") == ""
    assert subtitle_text_to_plain("  \n\t\n \r\n") == ""


def test_dialogue_that_looks_like_metadata_is_kept():   # regression
    srt = "\n".join([
        "1", "00:00:01,000 --> 00:00:02,000", "Note that the door is open", "",
        "2", "00:00:03,000 --> 00:00:04,000", "Style is everything here", "",
        "3", "00:00:05,000 --> 00:00:06,000", "Region 5 has fallen", "",
        "4", "00:00:07,000 --> 00:00:08,000", "WEBVTT stands for something", "",
        "5", "00:00:09,000 --> 00:00:10,000", "NOTE that this is shouted", "",
    ])
    assert subtitle_text_to_plain(srt) == ("Note that the door is open\nStyle is everything here\nRegion 5 has fallen\n"
                                           "WEBVTT stands for something\nNOTE that this is shouted")
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNOTE that\nSTYLE guide\nREGION 3\n"
    assert subtitle_text_to_plain(vtt) == "NOTE that\nSTYLE guide\nREGION 3"


def test_a_bare_number_that_is_dialogue_is_kept():   # regression
    srt = "1\n00:00:01,000 --> 00:00:02,000\nThe year was\n2024\n\n2\n00:00:03,000 --> 00:00:04,000\n7\n"
    assert subtitle_text_to_plain(srt) == "The year was\n2024\n7"


@pytest.mark.parametrize("spoken", ["3 < 5 and 7 > 2", "I <3 you", "x < y", "<3", "a <= b >= c", "1<2, 3>2"])
def test_angle_brackets_in_speech_are_not_tags(spoken):   # regression
    assert subtitle_text_to_plain(f"00:00:01.000 --> 00:00:02.000\n{spoken}\n") == spoken


def test_inbox_srt_roundtrip_end_to_end(inbox):
    body = "\n".join(["1", "00:00:01,000 --> 00:00:02,000", "Note that <i>this</i> works", "", "2",
                      "00:00:03,000 --> 00:00:04,000", "2024", ""])
    (inbox / "story.srt").write_text(body, encoding="utf-8")
    assert read_inbox_text(inbox, "story.srt") == "Note that this works\n2024"
