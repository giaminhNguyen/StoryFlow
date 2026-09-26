"""Multi-source ingestion service (roadmap 4.1): links / playlists / channels -> projects, with a ledger.

``add_sources`` turns what a person typed into one project per video inside ONE workflow:

* a video link -> one project; a playlist / channel -> the first ``limit`` videos of the listing (a channel is
  newest first; a PLAYLIST comes back in playlist order, so "the first N" are not the newest) and a
  ``source_feeds`` row remembering it. Listing goes through ``ctx.video_lister`` BEFORE any database
  transaction is opened, under one total deadline (``LIST_DEADLINE_SECONDS``) for the whole call;
* the ledger (``story_projects.video_id``) makes every video processed once: a video that already has a
  project (in this workflow, or in any workflow that was not cancelled) is reported as a duplicate and NOT
  added again, unless ``reprocess=True``;
* projects are created in listing order with strictly increasing ``created_at``, which is the order the
  orchestrator walks them in;
* one call creates at most ``MAX_NEW_PER_CALL`` projects: the rest is reported (``truncated``, ``not_added``)
  and picked up by the next call (the ledger skips what already exists).

``sync_feeds`` re-scans the stored feeds and adds only videos the ledger has not seen (the cursor), so a
channel can be topped up later without redoing anything. The re-scan looks at a wider window than the first
add (``limit`` x 3, at least 50) so videos published between two scans are not lost, and applies the feed's
stored ``min_duration_seconds``. Every feed report carries ``window_full`` (the listing returned as many videos
as were asked for, so older ones may exist beyond it) and ``order``; a listing that ended with errors is
reported under ``warnings`` (``partial_listing``), never silently. Adding to a FINISHED workflow re-opens it.
Only ``storyflow.errors`` types escape for expected problems; messages never contain paths or secrets.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .errors import CapacityUnavailable, InvalidState, NotFound, ValidationFailed
from .models import ChannelWorkflow, ChannelWorkflowStatus, SourceFeed, StoryProject, uid
from .pipeline import PipelineContext
from .services import _chunks, _is_unique_violation, slugify
from .sources import (
    CHANNEL, LOCAL, PLAYLIST, VIDEO, ParsedSource, SourceError, parse_source, read_inbox_text, _resolve_inbox_file,
)

logger = logging.getLogger(__name__)

_FRESH = {"execution_options": {"populate_existing": True}}
_S = ChannelWorkflowStatus
_INGEST_OK = (_S.DRAFT.value, _S.ACTIVE.value, _S.PAUSED.value, _S.FINISHED.value)
_IGNORED_WORKFLOWS = (_S.CANCELLED.value, _S.ABANDONED.value)
_MAX_TRIES = 5

DEFAULT_LIMIT = 10
MAX_LIMIT = 1000
MAX_NEW_PER_CALL = 1000            # projects created by ONE add_sources / sync_feeds call
MAX_SOURCES = 50
MAX_LANGUAGES = 8
LIST_DEADLINE_SECONDS = 300.0      # total time budget for all the channel / playlist listings of one call
SYNC_WINDOW_FACTOR = 3
SYNC_MIN_WINDOW = 50
MAX_REPORTED_DUPLICATES = 1000     # the list is capped, ``duplicates_count`` always has the real number


@dataclass
class _Item:
    video_id: str | None
    title: str | None
    extra: dict = field(default_factory=dict)      # source_config additions ("kind": "local", "file": ...)


@dataclass
class _FeedSpec:
    kind: str
    ref: str
    title: str | None
    limit: int | None
    languages: list | None
    min_duration: int | None = None


@dataclass
class _Group:
    """The videos of one source: ``feed`` is None for a directly added video / inbox file."""

    feed: _FeedSpec | None
    items: list
    listed: int = 0                  # videos the lister returned (before the duration filter)
    requested: int | None = None     # how many it was asked for
    partial: bool = False            # the listing ended with errors


@dataclass
class _Listing:
    title: str | None
    videos: list
    listed: int
    partial: bool


@dataclass(frozen=True)
class SourcesResult:
    workflow_id: str
    status: str                     # workflow status after the call
    changed: bool                   # at least one project was added
    added: list = field(default_factory=list)       # {"project_id","video_id","title","slug","feed_id"}
    duplicates: list = field(default_factory=list)  # {"video_id","title","reason","project_id"} (capped list)
    feeds: list = field(default_factory=list)       # {"id","kind","ref","title","listed","added","known",
                                                    #  "window_full","order","partial"}
    errors: list = field(default_factory=list)      # {"source","code","message"} (sync: feeds that could not be listed)
    reopened: bool = False
    truncated: bool = False         # more than MAX_NEW_PER_CALL new videos: the rest is in ``not_added``
    not_added: int = 0
    duplicates_count: int = 0       # every duplicate found, even when ``duplicates`` is capped
    warnings: list = field(default_factory=list)    # {"source","code","message"} e.g. partial_listing


def _sync_window(limit: int | None) -> int:
    if limit is None:
        return MAX_LIMIT
    return min(MAX_LIMIT, max(limit * SYNC_WINDOW_FACTOR, SYNC_MIN_WINDOW))


def _accepts_timeout(lister) -> bool:
    try:
        params = inspect.signature(lister.list_videos).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class SourceService:
    def __init__(self, ctx: PipelineContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ validation

    @staticmethod
    def _validate(sources, limit, languages, min_duration_seconds, reprocess):
        if not isinstance(sources, list) or not sources:
            raise ValidationFailed("sources must be a non-empty list", reason="sources_required")
        if len(sources) > MAX_SOURCES:
            raise ValidationFailed(f"at most {MAX_SOURCES} sources per call", reason="too_many_sources",
                                   max=MAX_SOURCES)
        parsed = []
        for i, text in enumerate(sources):
            try:
                parsed.append(parse_source(text))
            except SourceError as exc:
                raise ValidationFailed(str(exc), reason="invalid_source", index=i) from None
        # ``limit`` is required: "everything" is never a legal request (a channel can hold thousands of videos)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ValidationFailed(f"limit must be a whole number between 1 and {MAX_LIMIT}", reason="invalid_limit")
        if languages is not None:
            if (not isinstance(languages, list) or not 1 <= len(languages) <= MAX_LANGUAGES
                    or not all(isinstance(x, str) and 1 <= len(x.strip()) <= 16 for x in languages)):
                raise ValidationFailed("languages must be a short list of language codes", reason="invalid_languages")
            languages = [x.strip() for x in languages]
        if min_duration_seconds is not None and (
                isinstance(min_duration_seconds, bool) or not isinstance(min_duration_seconds, int)
                or not 0 <= min_duration_seconds <= 86400):
            raise ValidationFailed("min_duration_seconds must be between 0 and 86400", reason="invalid_duration")
        if not isinstance(reprocess, bool):
            raise ValidationFailed("reprocess must be true or false", reason="invalid_reprocess")
        return parsed, languages

    def _lister(self):
        lister = getattr(self.ctx, "video_lister", None)
        if lister is None:
            raise CapacityUnavailable("channel / playlist listing is not available (yt-dlp is not configured)",
                                      reason="lister_unavailable")
        return lister

    def _list(self, parsed: ParsedSource, limit, min_duration, *, index=None, deadline=None) -> _Listing:
        """One listing under the call's deadline; every error carries the source ``index`` when there is one."""
        where = {} if index is None else {"index": index}
        timeout = None
        if deadline is not None:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise CapacityUnavailable("listing the channels took too long; nothing was added",
                                          reason="lister_timeout", **where)
        lister = self._lister()
        try:
            if timeout is not None and _accepts_timeout(lister):
                listed = lister.list_videos(parsed, limit, timeout=timeout)
            else:
                listed = lister.list_videos(parsed, limit)
        except SourceError as exc:
            if exc.code in ("lister_unavailable", "lister_timeout"):
                raise CapacityUnavailable(str(exc), reason=exc.code, **where) from None
            raise ValidationFailed(str(exc), reason="source_unreadable", **where) from None
        videos = [v for v in listed.videos if min_duration is None or v.duration_seconds is None
                  or v.duration_seconds >= min_duration]
        return _Listing(listed.title, videos, len(listed.videos), bool(getattr(listed, "partial", False)))

    def _check_inbox(self, parsed) -> None:
        """``inbox:`` sources must exist and be readable NOW: a typo must not become a project that later pauses
        the whole batch. The inbox folder is created on first use."""
        locals_ = [(i, s) for i, s in enumerate(parsed) if s.kind == LOCAL]
        if not locals_:
            return
        inbox = getattr(self.ctx, "inbox_dir", None)
        if inbox is not None:
            try:
                Path(inbox).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        for i, src in locals_:
            if inbox is None or _resolve_inbox_file(inbox, src.ref) is None:
                raise ValidationFailed("the inbox file was not found in the inbox folder",
                                       reason="inbox_file_missing", index=i)
            text = read_inbox_text(inbox, src.ref)
            if text is None or not text.strip():
                raise ValidationFailed("the inbox file cannot be read (too big, not text, or empty)",
                                       reason="inbox_file_unreadable", index=i)

    # ------------------------------------------------------------------ public API

    def add_sources(self, workflow_id, sources, *, limit=DEFAULT_LIMIT, languages=None, reprocess=False,
                    min_duration_seconds=None) -> SourcesResult:
        parsed, languages = self._validate(sources, limit, languages, min_duration_seconds, reprocess)
        self._require_open(workflow_id)
        self._check_inbox(parsed)
        deadline = time.monotonic() + LIST_DEADLINE_SECONDS
        groups: list[_Group] = []
        for i, src in enumerate(parsed):
            if src.kind == VIDEO:
                groups.append(_Group(None, [_Item(src.ref, None)]))
            elif src.kind == LOCAL:
                stem = PurePosixPath(src.ref).stem
                groups.append(_Group(None, [_Item(None, stem, {"kind": "local", "file": src.ref})]))
            else:
                listing = self._list(src, limit, min_duration_seconds, index=i, deadline=deadline)
                groups.append(_Group(_FeedSpec(src.kind, src.ref, listing.title, limit, languages,
                                               min_duration_seconds),
                                     [_Item(v.video_id, v.title) for v in listing.videos],
                                     listing.listed, limit, listing.partial))
        return self._ingest(workflow_id, groups, reprocess=reprocess, languages=languages)

    def sync_feeds(self, workflow_id) -> SourcesResult:
        """Re-scan every stored feed of the workflow and add only videos the ledger has not seen."""
        self._require_open(workflow_id)
        with self.ctx.session_factory() as db:
            feeds = [(f.id, f.kind, f.ref, f.limit_count, f.languages, f.min_duration_seconds) for f in db.scalars(
                select(SourceFeed).where(SourceFeed.channel_workflow_id == workflow_id)
                .order_by(SourceFeed.created_at, SourceFeed.id), **_FRESH).all()]
            wf = self._workflow(db, workflow_id)
            status = wf.status
        if not feeds:
            return SourcesResult(workflow_id, status, False)
        self._lister()
        deadline = time.monotonic() + LIST_DEADLINE_SECONDS
        groups: list[_Group] = []
        errors: list[dict] = []
        failed_ids: dict[str, str] = {}
        for feed_id, kind, ref, limit, languages, min_duration in feeds:
            window = _sync_window(limit)
            try:
                listing = self._list(ParsedSource(kind, ref), window, min_duration, deadline=deadline)
            except (ValidationFailed, CapacityUnavailable) as exc:
                errors.append({"source": ref, "code": exc.details.get("reason", "lister_failed"),
                               "message": exc.message})
                failed_ids[feed_id] = exc.message
                continue
            groups.append(_Group(_FeedSpec(kind, ref, listing.title, limit, languages, min_duration),
                                 [_Item(v.video_id, v.title) for v in listing.videos],
                                 listing.listed, window, listing.partial))
        self._mark_feed_errors(failed_ids)
        result = self._ingest(workflow_id, groups, reprocess=False, languages=None) if groups else \
            SourcesResult(workflow_id, status, False)
        return SourcesResult(result.workflow_id, result.status, result.changed, result.added, result.duplicates,
                             result.feeds, errors, result.reopened, result.truncated, result.not_added,
                             result.duplicates_count, result.warnings)

    def workflow_brief(self, workflow_id) -> dict:
        """A few facts about the workflow, cheap at any size (API responses of big batches stay small):
        ``counts`` are the durable per-project statuses (active / completed / skipped / needs_attention)."""
        with self.ctx.session_factory() as db:
            wf = self._workflow(db, workflow_id)
            counts = {status: n for status, n in db.execute(
                select(StoryProject.status, func.count()).where(StoryProject.channel_workflow_id == workflow_id)
                .group_by(StoryProject.status)).all()}
            return {"id": wf.id, "name": wf.name, "status": wf.status, "status_reason": wf.status_reason,
                    "project_count": sum(counts.values()), "counts": counts}

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _workflow(db, workflow_id) -> ChannelWorkflow:
        wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
        if wf is None:
            raise NotFound("workflow not found", reason="workflow_not_found", workflow_id=workflow_id)
        return wf

    def _require_open(self, workflow_id) -> None:
        with self.ctx.session_factory() as db:
            wf = self._workflow(db, workflow_id)
            if wf.status not in _INGEST_OK:
                raise InvalidState(f"cannot add sources to a workflow in status {wf.status}",
                                   reason="workflow_closed", workflow_id=wf.id, status=wf.status)

    def _mark_feed_errors(self, failed: dict) -> None:
        if not failed:
            return
        with self.ctx.session_factory() as db:
            for feed_id, message in failed.items():
                feed = db.get(SourceFeed, feed_id)
                if feed is not None:
                    feed.status, feed.last_error, feed.updated_at = "error", str(message)[:500], self.ctx.clock()
            db.commit()

    def _ingest(self, workflow_id, groups, *, reprocess: bool, languages) -> SourcesResult:
        for _ in range(_MAX_TRIES):
            db = self.ctx.session_factory()
            try:
                out = self._ingest_once(db, workflow_id, groups, reprocess, languages)
            except IntegrityError as exc:
                db.rollback()
                if not _is_unique_violation(exc, "slug", "source_feeds"):
                    raise
                continue  # lost a slug / feed race: recompute against the new committed state
            finally:
                db.close()
            if out is not None:
                return out
        raise InvalidState("workflow is changing concurrently; retry the command",
                           reason="concurrent_modification", workflow_id=workflow_id)

    def _ingest_once(self, db, workflow_id, groups, reprocess, languages) -> SourcesResult | None:
        now = self.ctx.clock()
        wf = self._workflow(db, workflow_id)
        if wf.status not in _INGEST_OK:
            raise InvalidState(f"cannot add sources to a workflow in status {wf.status}", reason="workflow_closed",
                               workflow_id=wf.id, status=wf.status)
        # first write of the txn: takes the write lock and proves the status did not change under us
        touched = db.execute(update(ChannelWorkflow)
                             .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status == wf.status)
                             .values(updated_at=now).execution_options(synchronize_session=False))
        if touched.rowcount != 1:
            db.rollback()
            return None

        ids = [it.video_id for g in groups for it in g.items if it.video_id]
        known: dict[str, tuple[str, str]] = {}
        if ids and not reprocess:
            for chunk in _chunks(ids):
                rows = db.execute(
                    select(StoryProject.video_id, StoryProject.id, StoryProject.channel_workflow_id)
                    .join(ChannelWorkflow, ChannelWorkflow.id == StoryProject.channel_workflow_id)
                    .where(StoryProject.video_id.in_(chunk), ChannelWorkflow.status.notin_(_IGNORED_WORKFLOWS))
                    .order_by(StoryProject.created_at)).all()
                for video_id, project_id, wid in rows:
                    known.setdefault(video_id, (project_id, wid))
        # Windows treats Story.txt and story.txt as one file: compare local file names case-insensitively
        local_files = {str((c or {}).get("file")).lower() for (c,) in db.execute(
            select(StoryProject.source_config).where(StoryProject.channel_workflow_id == workflow_id,
                                                     StoryProject.source_config.is_not(None))).all()}

        # Slugs: read every taken slug ONCE (one index scan) and allocate from memory, instead of one LIKE scan
        # per inserted project (which made a 1000-video batch quadratic).
        taken = {s for (s,) in db.execute(select(StoryProject.slug).where(StoryProject.slug.is_not(None))).all()}
        next_suffix: dict[str, int] = {}

        def free_slug(base: str) -> str:
            if base not in taken:
                taken.add(base)
                return base
            n = next_suffix.get(base, 2)
            while f"{base}-{n}" in taken:
                n += 1
            next_suffix[base] = n + 1
            taken.add(f"{base}-{n}")
            return f"{base}-{n}"

        # Order = created_at. Stamp strictly AFTER every project already in the workflow, so a second call made
        # within the same instant (or one whose stamps overrun the clock) can never interleave with the first.
        latest = db.scalar(select(func.max(StoryProject.created_at))
                           .where(StoryProject.channel_workflow_id == workflow_id))
        stamp_base = latest if latest is not None and latest > now else now

        added, duplicates, feed_reports, warnings = [], [], [], []
        duplicates_count = not_added = 0
        seen: set[str] = set()
        counter = 0

        def duplicate(entry: dict) -> None:
            nonlocal duplicates_count
            duplicates_count += 1
            if len(duplicates) < MAX_REPORTED_DUPLICATES:
                duplicates.append(entry)

        for group in groups:
            spec = group.feed
            feed = self._upsert_feed(db, workflow_id, spec, now) if spec is not None else None
            n_added = 0
            for item in group.items:
                if item.video_id is None:                                   # local inbox file
                    file = str(item.extra.get("file")).lower()
                    if file in local_files and not reprocess:
                        duplicate({"video_id": None, "title": item.title, "reason": "in_workflow",
                                   "project_id": None})
                        continue
                elif item.video_id in seen:
                    duplicate({"video_id": item.video_id, "title": item.title, "reason": "repeated",
                               "project_id": None})
                    continue
                elif item.video_id in known:
                    project_id, wid = known[item.video_id]
                    duplicate({"video_id": item.video_id, "title": item.title, "project_id": project_id,
                               "reason": "in_workflow" if wid == workflow_id else "already_processed"})
                    continue
                if len(added) >= MAX_NEW_PER_CALL:      # never silently: counted and flagged in the result
                    not_added += 1
                    continue
                if item.video_id is None:
                    local_files.add(str(item.extra.get("file")).lower())
                else:
                    seen.add(item.video_id)
                title = " ".join((item.title or "").split()) or (
                    f"YouTube {item.video_id}" if item.video_id else "Local file")
                title = title[:255]
                config = dict(item.extra) if item.extra else {"kind": "video", "video_id": item.video_id}
                if languages and config.get("kind") != "local":
                    config["languages"] = languages
                elif feed is not None and feed.languages and config.get("kind") != "local":
                    config["languages"] = feed.languages
                counter += 1
                stamp = stamp_base + timedelta(microseconds=counter)   # keeps listing order
                project = StoryProject(
                    id=uid(), channel_workflow_id=workflow_id, title=title, slug=free_slug(slugify(title)),
                    video_id=item.video_id, feed_id=feed.id if feed is not None else None, source_config=config,
                    created_at=stamp, updated_at=stamp)
                db.add(project)
                n_added += 1
                added.append({"project_id": project.id, "video_id": item.video_id, "title": title,
                              "slug": project.slug, "feed_id": project.feed_id})
            if feed is not None:
                db.flush()      # one flush per source (not per project) so the count below sees the new rows
                feed.known_count = db.scalar(select(func.count()).select_from(StoryProject)
                                             .where(StoryProject.feed_id == feed.id)) or 0
                window_full = group.requested is not None and group.listed >= group.requested
                feed_reports.append({"id": feed.id, "kind": feed.kind, "ref": feed.ref, "title": feed.title,
                                     "listed": len(group.items), "added": n_added, "known": feed.known_count,
                                     "window_full": window_full, "partial": group.partial,
                                     "order": "newest_first" if feed.kind == CHANNEL else "playlist_order"})
                if group.partial:
                    warnings.append({"source": feed.ref, "code": "partial_listing",
                                     "message": "the listing ended with errors: some videos may be missing "
                                                "(run sync later to pick them up)"})
        db.flush()

        reopened = False
        status = wf.status
        if added and wf.status == _S.FINISHED.value:
            res = db.execute(update(ChannelWorkflow)
                             .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status == _S.FINISHED.value)
                             .values(status=_S.ACTIVE.value, finished_at=None, updated_at=now)
                             .execution_options(synchronize_session=False))
            reopened = res.rowcount == 1
            status = _S.ACTIVE.value if reopened else status
        db.commit()
        logger.info("sources_added workflow=%s added=%d duplicates=%d feeds=%d reopened=%s truncated=%s",
                    workflow_id, len(added), duplicates_count, len(feed_reports), reopened, bool(not_added))
        return SourcesResult(workflow_id, status, bool(added), added, duplicates, feed_reports, [], reopened,
                             bool(not_added), not_added, duplicates_count, warnings)

    @staticmethod
    def _upsert_feed(db, workflow_id, spec: _FeedSpec, now) -> SourceFeed:
        feed = db.scalar(select(SourceFeed).where(
            SourceFeed.channel_workflow_id == workflow_id, SourceFeed.kind == spec.kind, SourceFeed.ref == spec.ref),
            **_FRESH)
        if feed is None:
            feed = SourceFeed(channel_workflow_id=workflow_id, kind=spec.kind, ref=spec.ref, created_at=now)
            db.add(feed)
        if spec.title:
            feed.title = spec.title[:255]
        feed.limit_count = spec.limit
        feed.min_duration_seconds = spec.min_duration
        if spec.languages is not None:
            feed.languages = spec.languages
        feed.status, feed.last_error, feed.last_scanned_at, feed.updated_at = "active", None, now, now
        db.flush()
        return feed


__all__ = ["SourceService", "SourcesResult", "DEFAULT_LIMIT", "MAX_LIMIT", "MAX_NEW_PER_CALL", "MAX_SOURCES",
           "CHANNEL", "PLAYLIST"]
