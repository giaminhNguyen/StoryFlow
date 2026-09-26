"""Multi-source ingestion service (roadmap 4.1): links / playlists / channels -> projects, with a ledger.

``add_sources`` turns what a person typed into one project per video inside ONE workflow:

* a video link -> one project; a playlist / channel -> the newest ``limit`` videos (listing goes through
  ``ctx.video_lister`` BEFORE any database transaction is opened) and a ``source_feeds`` row remembering it;
* the ledger (``story_projects.video_id``) makes every video processed once: a video that already has a
  project (in this workflow, or in any workflow that was not cancelled) is reported as a duplicate and NOT
  added again, unless ``reprocess=True``;
* projects are created in listing order (newest first) with strictly increasing ``created_at``, which is the
  order the orchestrator walks them in.

``sync_feeds`` re-scans the stored feeds and adds only videos the ledger has not seen (the cursor), so a
channel can be topped up later without redoing anything. Adding to a FINISHED workflow re-opens it.
Only ``storyflow.errors`` types escape for expected problems; messages never contain paths or secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .errors import CapacityUnavailable, InvalidState, NotFound, ValidationFailed
from .models import ChannelWorkflow, ChannelWorkflowStatus, SourceFeed, StoryProject
from .pipeline import PipelineContext
from .services import _chunks, _is_unique_violation, slugify
from .sources import CHANNEL, LOCAL, PLAYLIST, VIDEO, ParsedSource, SourceError, VideoRef, parse_source

logger = logging.getLogger(__name__)

_FRESH = {"execution_options": {"populate_existing": True}}
_S = ChannelWorkflowStatus
_INGEST_OK = (_S.DRAFT.value, _S.ACTIVE.value, _S.PAUSED.value, _S.FINISHED.value)
_IGNORED_WORKFLOWS = (_S.CANCELLED.value, _S.ABANDONED.value)
_MAX_TRIES = 5

DEFAULT_LIMIT = 10
MAX_LIMIT = 1000
MAX_SOURCES = 50
MAX_LANGUAGES = 8


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


@dataclass(frozen=True)
class SourcesResult:
    workflow_id: str
    status: str                     # workflow status after the call
    changed: bool                   # at least one project was added
    added: list = field(default_factory=list)       # {"project_id","video_id","title","slug","feed_id"}
    duplicates: list = field(default_factory=list)  # {"video_id","title","reason","project_id"}
    feeds: list = field(default_factory=list)       # {"id","kind","ref","title","listed","added","known"}
    errors: list = field(default_factory=list)      # {"source","code","message"} (sync: feeds that could not be listed)
    reopened: bool = False


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
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT):
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

    def _list(self, parsed: ParsedSource, limit, min_duration):
        try:
            listed = self._lister().list_videos(parsed, limit)
        except SourceError as exc:
            if exc.code in ("lister_unavailable", "lister_timeout"):
                raise CapacityUnavailable(str(exc), reason=exc.code) from None
            raise ValidationFailed(str(exc), reason="source_unreadable") from None
        videos = [v for v in listed.videos if min_duration is None or v.duration_seconds is None
                  or v.duration_seconds >= min_duration]
        return listed.title, videos

    # ------------------------------------------------------------------ public API

    def add_sources(self, workflow_id, sources, *, limit=DEFAULT_LIMIT, languages=None, reprocess=False,
                    min_duration_seconds=None) -> SourcesResult:
        parsed, languages = self._validate(sources, limit, languages, min_duration_seconds, reprocess)
        self._require_open(workflow_id)
        groups: list[tuple[_FeedSpec | None, list[_Item]]] = []
        for src in parsed:
            if src.kind == VIDEO:
                groups.append((None, [_Item(src.ref, None)]))
            elif src.kind == LOCAL:
                stem = PurePosixPath(src.ref).stem
                groups.append((None, [_Item(None, stem, {"kind": "local", "file": src.ref})]))
            else:
                title, videos = self._list(src, limit, min_duration_seconds)
                groups.append((_FeedSpec(src.kind, src.ref, title, limit, languages),
                               [_Item(v.video_id, v.title) for v in videos]))
        return self._ingest(workflow_id, groups, reprocess=reprocess, languages=languages)

    def sync_feeds(self, workflow_id) -> SourcesResult:
        """Re-scan every stored feed of the workflow and add only videos the ledger has not seen."""
        self._require_open(workflow_id)
        with self.ctx.session_factory() as db:
            feeds = [(f.id, f.kind, f.ref, f.limit_count, f.languages) for f in db.scalars(
                select(SourceFeed).where(SourceFeed.channel_workflow_id == workflow_id)
                .order_by(SourceFeed.created_at, SourceFeed.id), **_FRESH).all()]
            wf = self._workflow(db, workflow_id)
            status = wf.status
        if not feeds:
            return SourcesResult(workflow_id, status, False)
        self._lister()
        groups: list[tuple[_FeedSpec | None, list[_Item]]] = []
        errors: list[dict] = []
        failed_ids: dict[str, str] = {}
        for feed_id, kind, ref, limit, languages in feeds:
            try:
                title, videos = self._list(ParsedSource(kind, ref), limit, None)
            except (ValidationFailed, CapacityUnavailable) as exc:
                errors.append({"source": ref, "code": exc.details.get("reason", "lister_failed"),
                               "message": exc.message})
                failed_ids[feed_id] = exc.message
                continue
            groups.append((_FeedSpec(kind, ref, title, limit, languages), [_Item(v.video_id, v.title) for v in videos]))
        self._mark_feed_errors(failed_ids)
        result = self._ingest(workflow_id, groups, reprocess=False, languages=None) if groups else \
            SourcesResult(workflow_id, status, False)
        return SourcesResult(result.workflow_id, result.status, result.changed, result.added, result.duplicates,
                             result.feeds, errors, result.reopened)

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

        ids = [it.video_id for _, items in groups for it in items if it.video_id]
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
        local_files = {(c or {}).get("file") for (c,) in db.execute(
            select(StoryProject.source_config).where(StoryProject.channel_workflow_id == workflow_id,
                                                     StoryProject.source_config.is_not(None))).all()}

        # Order = created_at. Stamp strictly AFTER every project already in the workflow, so a second call made
        # within the same instant (or one whose stamps overrun the clock) can never interleave with the first.
        latest = db.scalar(select(func.max(StoryProject.created_at))
                           .where(StoryProject.channel_workflow_id == workflow_id))
        stamp_base = latest if latest is not None and latest > now else now

        added, duplicates, feed_reports = [], [], []
        seen: set[str] = set()
        counter = 0
        for spec, items in groups:
            feed = self._upsert_feed(db, workflow_id, spec, now) if spec is not None else None
            n_added = 0
            for item in items:
                if item.video_id is None:                                   # local inbox file
                    file = item.extra.get("file")
                    if file in local_files and not reprocess:
                        duplicates.append({"video_id": None, "title": item.title, "reason": "in_workflow",
                                           "project_id": None})
                        continue
                    local_files.add(file)
                elif item.video_id in seen:
                    duplicates.append({"video_id": item.video_id, "title": item.title, "reason": "repeated",
                                       "project_id": None})
                    continue
                elif item.video_id in known:
                    project_id, wid = known[item.video_id]
                    duplicates.append({"video_id": item.video_id, "title": item.title, "project_id": project_id,
                                       "reason": "in_workflow" if wid == workflow_id else "already_processed"})
                    continue
                if item.video_id is not None:
                    seen.add(item.video_id)
                title = (item.title or (f"YouTube {item.video_id}" if item.video_id else "Local file"))[:255]
                config = dict(item.extra) if item.extra else {"kind": "video", "video_id": item.video_id}
                if languages and config.get("kind") != "local":
                    config["languages"] = languages
                elif feed is not None and feed.languages and config.get("kind") != "local":
                    config["languages"] = feed.languages
                counter += 1
                stamp = stamp_base + timedelta(microseconds=counter)   # keeps listing order (newest first)
                project = StoryProject(
                    channel_workflow_id=workflow_id, title=title, slug=self._free_slug(db, slugify(title)),
                    video_id=item.video_id, feed_id=feed.id if feed is not None else None, source_config=config,
                    created_at=stamp, updated_at=stamp)
                db.add(project)
                db.flush()
                n_added += 1
                added.append({"project_id": project.id, "video_id": item.video_id, "title": title,
                              "slug": project.slug, "feed_id": project.feed_id})
            if feed is not None:
                feed.known_count = db.scalar(select(func.count()).select_from(StoryProject)
                                             .where(StoryProject.feed_id == feed.id)) or 0
                feed_reports.append({"id": feed.id, "kind": feed.kind, "ref": feed.ref, "title": feed.title,
                                     "listed": len(items), "added": n_added, "known": feed.known_count})

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
        logger.info("sources_added workflow=%s added=%d duplicates=%d feeds=%d reopened=%s", workflow_id, len(added),
                    len(duplicates), len(feed_reports), reopened)
        return SourcesResult(workflow_id, status, bool(added), added, duplicates, feed_reports, [], reopened)

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
        if spec.languages is not None:
            feed.languages = spec.languages
        feed.status, feed.last_error, feed.last_scanned_at, feed.updated_at = "active", None, now, now
        db.flush()
        return feed

    @staticmethod
    def _free_slug(db, base: str) -> str:
        taken = set(db.scalars(select(StoryProject.slug).where(
            (StoryProject.slug == base) | StoryProject.slug.like(base + "-%")), **_FRESH).all())
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"


__all__ = ["SourceService", "SourcesResult", "DEFAULT_LIMIT", "MAX_LIMIT", "MAX_SOURCES", "CHANNEL", "PLAYLIST"]
